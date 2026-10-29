// 「不改程式」自動埋點驗證用的 .NET 8 應用（e2e/local-k3s/sdk-inject/dotnet.yaml）：程式裡沒有任何 OpenTelemetry，只掛自動埋點。
// log 用內建 JSON console＋IncludeScopes（跟 app-common chart 一樣用環境變數設定），trace_id、span_id 在 scope 裡。
// 每個請求帶 rid（英數字），log 訊息都寫 rid，驗證腳本用 rid 在 ES 找 log，再用 log 的 trace_id／span_id 找 span。
//   /hop?rid=&next=url1,url2   印「hop」（含收到的 traceparent），有 next 就用 HttpClient 呼叫下一跳（剩下的 next 往下傳）
//   /leaf?rid=                 印「leaf」，子工作打回自己用
//   /async、/taskrun、/pool、/thread?rid=   子工作在 await 之後、Task.Run、ThreadPool.QueueUserWorkItem、自己 new Thread 裡印 log、呼叫 /leaf
//   /kafka?rid=                把 rid 送進 Kafka topic sdk-dotnet；KAFKA_CONSUME=1 的那台有一條執行緒消費：印 log、呼叫 /leaf
//   背景排程                    每 15 秒印「tick」、呼叫 /leaf（沒有上游請求）
using Confluent.Kafka;

var svc = Environment.GetEnvironmentVariable("OTEL_SERVICE_NAME") ?? "sdk-dotnet";
var kafka = Environment.GetEnvironmentVariable("KAFKA") ?? "kafka:9092";
const string self = "http://localhost:8080";
var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();
var log = app.Services.GetRequiredService<ILoggerFactory>().CreateLogger("sdk-app");
var http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
using var producer = new ProducerBuilder<string, string>(new ProducerConfig { BootstrapServers = kafka }).Build();

async Task<string> Get(string url)
{
    try { return await http.GetStringAsync(url); }
    catch (Exception e) { log.LogError(e, "call {Url} failed", url); return "error"; }
}
Task<string> Child(string rid)
{
    log.LogInformation("child rid {Rid}", rid);
    return Get($"{self}/leaf?rid={rid}");
}

app.MapGet("/hop", async (string rid, string? next, HttpRequest req) =>
{
    log.LogInformation("hop rid {Rid} svc {Svc} tp {Tp}", rid, svc, req.Headers["traceparent"].ToString());
    if (string.IsNullOrEmpty(next)) return "ok";
    var parts = next.Split(',', 2);
    return await Get($"{parts[0]}/hop?rid={rid}" + (parts.Length > 1 ? $"&next={parts[1]}" : ""));
});
app.MapGet("/leaf", (string rid, HttpRequest req) =>
{
    log.LogInformation("leaf rid {Rid} svc {Svc} tp {Tp}", rid, svc, req.Headers["traceparent"].ToString());
    return "ok";
});
app.MapGet("/async", async (string rid) =>
{
    log.LogInformation("parent rid {Rid}", rid);
    await Task.Delay(50);
    return await Child(rid);
});
app.MapGet("/taskrun", async (string rid) =>
{
    log.LogInformation("parent rid {Rid}", rid);
    return await Task.Run(() => Child(rid).Result);
});
app.MapGet("/pool", async (string rid) =>
{
    log.LogInformation("parent rid {Rid}", rid);
    var done = new TaskCompletionSource<string>();
    ThreadPool.QueueUserWorkItem(_ => done.SetResult(Child(rid).Result));
    return await done.Task;
});
app.MapGet("/thread", (string rid) =>
{
    log.LogInformation("parent rid {Rid}", rid);
    string? r = null;
    var t = new Thread(() => r = Child(rid).Result);
    t.Start();
    t.Join(10_000);
    return r;
});
app.MapGet("/kafka", async (string rid) =>
{
    log.LogInformation("produce rid {Rid}", rid);
    await producer.ProduceAsync("sdk-dotnet", new Message<string, string> { Key = rid, Value = rid });
    return "sent";
});

if (Environment.GetEnvironmentVariable("KAFKA_CONSUME") == "1")
{
    new Thread(() =>
    {
        using var c = new ConsumerBuilder<string, string>(new ConsumerConfig
        {
            BootstrapServers = kafka, GroupId = "sdk-dotnet", AutoOffsetReset = AutoOffsetReset.Earliest,
        }).Build();
        c.Subscribe("sdk-dotnet");
        while (true)
        {
            var m = c.Consume(TimeSpan.FromSeconds(1));
            if (m == null) continue;
            log.LogInformation("consumed rid {Rid}", m.Message.Value);
            Get($"{self}/leaf?rid={m.Message.Value}").Wait();
        }
    }) { IsBackground = true, Name = "kafka-consumer" }.Start();
}

var n = 0;
_ = Task.Run(async () =>
{
    await Task.Delay(20_000);
    while (true)
    {
        var rid = $"tick{svc.Replace("-", "")}{++n}";
        log.LogInformation("tick rid {Rid}", rid);
        await Get($"{self}/leaf?rid={rid}");
        await Task.Delay(15_000);
    }
});

app.Run("http://0.0.0.0:8080");
