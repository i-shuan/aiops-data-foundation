// 指標案例驗證用的真的 Java 應用（e2e/local-k3s/java-app.yaml）：訂單 API，每個請求從 HikariCP 連線池拿連線查 DB（H2 記憶體資料庫）。
// 掛 OpenTelemetry Java agent，跟 app-common chart 一樣用 Prometheus 格式開 metrics，確認 JVM、連線池的指標名稱。
// 兩個開關（在 Pod 裡 touch／rm）：
//   /tmp/leak      記憶體洩漏：每個請求往一個永遠不清的 list 放 N KB（N 寫在檔案裡，預設 12；全域快取忘了清）
//   /tmp/leakconn  連線洩漏：拿了連線不還（例外路徑忘了 close），連線池很快被拿光，後面的請求排隊、逾時
import com.sun.net.httpserver.HttpServer;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import java.net.InetSocketAddress;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadLocalRandom;

public class OrderApp {
    static final List<byte[]> CACHE = new ArrayList<>();

    public static void main(String[] args) throws Exception {
        HikariConfig cfg = new HikariConfig();
        cfg.setJdbcUrl("jdbc:h2:mem:orders;DB_CLOSE_DELAY=-1");
        cfg.setPoolName("orders-db");
        cfg.setMaximumPoolSize(5);
        cfg.setConnectionTimeout(2000);
        HikariDataSource ds = new HikariDataSource(cfg);
        try (Connection c = ds.getConnection(); Statement s = c.createStatement()) {
            s.execute("create table lots(id int primary key, state varchar(20))");
            for (int i = 0; i < 1000; i++) s.execute("insert into lots values(" + i + ", 'RUN')");
        }
        HttpServer srv = HttpServer.create(new InetSocketAddress(8080), 0);
        srv.setExecutor(Executors.newFixedThreadPool(16));
        srv.createContext("/orders", ex -> {
            int code = 200;
            String body = "ok";
            try {
                Path leak = Path.of("/tmp/leak");
                if (Files.exists(leak)) {
                    String kb = Files.readString(leak).trim();
                    synchronized (CACHE) { CACHE.add(new byte[(kb.isEmpty() ? 12 : Integer.parseInt(kb)) * 1024]); }
                }
                Connection c = ds.getConnection();
                try (PreparedStatement p = c.prepareStatement("select state from lots where id = ?")) {
                    p.setInt(1, ThreadLocalRandom.current().nextInt(1000));
                    p.executeQuery().next();
                }
                if (!Files.exists(Path.of("/tmp/leakconn"))) c.close();
                byte[] garbage = new byte[32 * 1024];   // 平常的垃圾，讓 GC 有事做
                body = "ok " + garbage.length;
            } catch (Exception e) {
                code = 500;
                body = String.valueOf(e.getMessage());
            }
            byte[] b = body.getBytes();
            ex.sendResponseHeaders(code, b.length);
            ex.getResponseBody().write(b);
            ex.close();
        });
        srv.start();
        System.out.println("order-app started");
    }
}
