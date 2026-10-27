// 「不改程式」自動埋點驗證用的 Java 應用（e2e/local-k3s/sdk-inject/java.yaml）：程式裡沒有任何 OpenTelemetry，只掛 Java agent。
// log 用 logback＋LogstashEncoder（跟 sdk/standards/java/logback-spring.xml 一樣），trace_id、span_id 由 agent 放進 MDC。
// 每個請求帶 rid（英數字），log 訊息都寫 rid，驗證腳本用 rid 在 ES 找 log，再用 log 的 trace_id／span_id 找 span。
//   /hop?rid=&next=url1,url2   印「hop」（含收到的 traceparent），有 next 就用 java.net.http.HttpClient 呼叫下一跳（剩下的 next 往下傳）
//   /leaf?rid=                 印「leaf」，子工作打回自己用
//   /pool、/cf、/thread?rid=   子工作在 ExecutorService、CompletableFuture、自己 new Thread 裡印 log、呼叫 /leaf
//   /kafka?rid=                把 rid 送進 Kafka topic sdk-java；KAFKA_CONSUME=1 的那台有一條執行緒消費：印 log、呼叫 /leaf
//   背景排程                    每 15 秒印「tick」、呼叫 /leaf（沒有上游請求）
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.URLDecoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class SdkApp {
    static final Logger log = LoggerFactory.getLogger("sdk-app");
    static final String SVC = System.getenv().getOrDefault("OTEL_SERVICE_NAME", "sdk-java");
    static final String SELF = "http://localhost:8080";
    static final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
    static final ExecutorService pool = Executors.newFixedThreadPool(4);
    static final String KAFKA = System.getenv().getOrDefault("KAFKA", "kafka:9092");
    static KafkaProducer<String, String> producer;

    public static void main(String[] args) throws Exception {
        HttpServer srv = HttpServer.create(new InetSocketAddress(8080), 0);
        srv.setExecutor(Executors.newFixedThreadPool(8));
        srv.createContext("/hop", ex -> handle(ex, q -> {
            log.info("hop rid {} svc {} tp {}", q.get("rid"), SVC, ex.getRequestHeaders().getFirst("traceparent"));
            String next = q.getOrDefault("next", "");
            if (next.isEmpty()) return "ok";
            String[] parts = next.split(",", 2);
            return get(parts[0] + "/hop?rid=" + q.get("rid") + (parts.length > 1 ? "&next=" + parts[1] : ""));
        }));
        srv.createContext("/leaf", ex -> handle(ex, q -> {
            log.info("leaf rid {} svc {} tp {}", q.get("rid"), SVC, ex.getRequestHeaders().getFirst("traceparent"));
            return "ok";
        }));
        srv.createContext("/pool", ex -> handle(ex, q -> {
            log.info("parent rid {}", q.get("rid"));
            return pool.submit(() -> child(q.get("rid"))).get(10, TimeUnit.SECONDS);
        }));
        srv.createContext("/cf", ex -> handle(ex, q -> {
            log.info("parent rid {}", q.get("rid"));
            return CompletableFuture.supplyAsync(() -> child(q.get("rid"))).get(10, TimeUnit.SECONDS);
        }));
        srv.createContext("/thread", ex -> handle(ex, q -> {
            log.info("parent rid {}", q.get("rid"));
            String[] out = new String[1];
            Thread t = new Thread(() -> out[0] = child(q.get("rid")));
            t.start();
            t.join(10_000);
            return out[0];
        }));
        srv.createContext("/kafka", ex -> handle(ex, q -> {
            log.info("produce rid {}", q.get("rid"));
            producer.send(new ProducerRecord<>("sdk-java", q.get("rid"), q.get("rid"))).get(10, TimeUnit.SECONDS);
            return "sent";
        }));
        srv.start();

        Properties p = new Properties();
        p.put("bootstrap.servers", KAFKA);
        p.put("key.serializer", "org.apache.kafka.common.serialization.StringSerializer");
        p.put("value.serializer", "org.apache.kafka.common.serialization.StringSerializer");
        producer = new KafkaProducer<>(p);
        if ("1".equals(System.getenv("KAFKA_CONSUME"))) {
            Thread c = new Thread(SdkApp::consume, "kafka-consumer");
            c.setDaemon(true);
            c.start();
        }

        AtomicLong n = new AtomicLong();
        Executors.newSingleThreadScheduledExecutor().scheduleAtFixedRate(() -> {
            String rid = "tick" + SVC.replace("-", "") + n.incrementAndGet();
            log.info("tick rid {}", rid);
            get(SELF + "/leaf?rid=" + rid);
        }, 20, 15, TimeUnit.SECONDS);
        log.info("{} started", SVC);
    }

    static void consume() {
        Properties p = new Properties();
        p.put("bootstrap.servers", KAFKA);
        p.put("group.id", "sdk-java");
        p.put("auto.offset.reset", "earliest");
        p.put("key.deserializer", "org.apache.kafka.common.serialization.StringDeserializer");
        p.put("value.deserializer", "org.apache.kafka.common.serialization.StringDeserializer");
        try (KafkaConsumer<String, String> c = new KafkaConsumer<>(p)) {
            c.subscribe(List.of("sdk-java"));
            while (true) {
                for (ConsumerRecord<String, String> r : c.poll(Duration.ofSeconds(1))) {
                    log.info("consumed rid {}", r.value());
                    get(SELF + "/leaf?rid=" + r.value());
                }
            }
        } catch (Exception e) {
            log.error("consumer stopped", e);
        }
    }

    static String child(String rid) {
        log.info("child rid {}", rid);
        return get(SELF + "/leaf?rid=" + rid);
    }

    static String get(String url) {
        try {
            return http.send(HttpRequest.newBuilder(URI.create(url)).timeout(Duration.ofSeconds(10)).build(),
                    HttpResponse.BodyHandlers.ofString()).body();
        } catch (Exception e) {
            log.error("call {} failed", url, e);
            return "error";
        }
    }

    interface Handler { String run(Map<String, String> q) throws Exception; }

    static void handle(HttpExchange ex, Handler h) {
        Map<String, String> q = new HashMap<>();
        String raw = ex.getRequestURI().getRawQuery();
        if (raw != null) for (String kv : raw.split("&")) {
            int i = kv.indexOf('=');
            if (i > 0) q.put(kv.substring(0, i), URLDecoder.decode(kv.substring(i + 1), StandardCharsets.UTF_8));
        }
        int code = 200;
        String body;
        try { body = h.run(q); } catch (Exception e) { code = 500; body = String.valueOf(e); log.error("rid {} failed", q.get("rid"), e); }
        try {
            byte[] b = body.getBytes(StandardCharsets.UTF_8);
            ex.sendResponseHeaders(code, b.length);
            ex.getResponseBody().write(b);
        } catch (Exception ignored) {
        } finally {
            ex.close();
        }
    }
}
