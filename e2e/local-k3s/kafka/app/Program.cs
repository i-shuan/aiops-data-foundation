// Kafka 的 trace 驗證用的 .NET 8 應用（e2e/local-k3s/kafka/apps.yaml），跑兩種角色（環境變數 APP_MODE）：
//   producer：訂單 API。GET /orders/{id} 收到就把訂單送進 Kafka 的 orders topic（Confluent.Kafka）
//   consumer：消費者（group order-push），讀 orders、寫進 PostgreSQL（Npgsql）
// 程式裡沒有任何 OpenTelemetry：trace 全靠自動埋點（跟舊專案一樣），看 Kafka 前後能不能串成同一個 trace。
// 實測：自動埋點對 Kafka 消費者只開「receive」span（新的 trace，只用 link 指回生產者），處理訊息時查的 DB 自成一個 trace，
// 跟訊息串不起來（docs/trace/README.md 場景四）。
// 消費者的開關（Pod 裡 /ctl 底下的檔案）：
//   /ctl/slow   每筆訊息多處理 N 毫秒（N 寫在檔案裡，預設 200）：消費者跟不上，訊息在 Kafka 裡排隊
//   /ctl/fail   寫 DB 失敗（寫到不存在的表），程式 catch 掉、記一行 error log 繼續下一筆
using Confluent.Kafka;
using Npgsql;

var mode = Environment.GetEnvironmentVariable("APP_MODE") ?? "producer";
var bootstrap = Environment.GetEnvironmentVariable("KAFKA") ?? "kafka:9092";
var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();
var log = app.Services.GetRequiredService<ILoggerFactory>().CreateLogger(mode);

if (mode == "producer")
{
    using var producer = new ProducerBuilder<string, string>(new ProducerConfig { BootstrapServers = bootstrap, Acks = Acks.Leader }).Build();
    app.MapGet("/orders/{id:int}", async (int id) =>
    {
        var r = await producer.ProduceAsync("orders", new Message<string, string> { Key = $"{id}", Value = $"{{\"id\":{id},\"qty\":1}}" });
        log.LogInformation("order {OrderId} sent to partition {Partition}", id, r.Partition.Value);
        return Results.Ok(new { id });
    });
    app.Urls.Add("http://0.0.0.0:8080");
    app.Run();
    return;
}

// ── consumer ──
var ds = NpgsqlDataSource.Create("Host=localhost;Username=postgres;Password=test-only;Database=postgres;Maximum Pool Size=5;Timeout=2");
for (var i = 0; ; i++)
{
    try
    {
        await using var c = await ds.OpenConnectionAsync();
        await using var cmd = new NpgsqlCommand("create table if not exists orders(id int, payload text, at timestamptz default now())", c);
        await cmd.ExecuteNonQueryAsync();
        break;
    }
    catch when (i < 30) { await Task.Delay(1000); }   // 等 PostgreSQL 起來
}

var worker = new Thread(() =>
{
    var cfg = new ConsumerConfig
    {
        BootstrapServers = bootstrap, GroupId = "order-push",
        AutoOffsetReset = AutoOffsetReset.Earliest, EnableAutoCommit = true,
    };
    using var consumer = new ConsumerBuilder<string, string>(cfg).Build();
    consumer.Subscribe("orders");
    while (true)
    {
        var msg = consumer.Consume(TimeSpan.FromSeconds(1));
        if (msg == null) continue;
        try
        {
            if (File.Exists("/ctl/slow"))
                Thread.Sleep(int.TryParse(File.ReadAllText("/ctl/slow").Trim(), out var ms) ? ms : 200);
            var table = File.Exists("/ctl/fail") ? "orders_missing" : "orders";
            using var c = ds.OpenConnection();
            using var cmd = new NpgsqlCommand($"insert into {table}(id, payload) values ($1, $2)", c);
            cmd.Parameters.AddWithValue(int.Parse(msg.Message.Key));
            cmd.Parameters.AddWithValue(msg.Message.Value);
            cmd.ExecuteNonQuery();
            log.LogInformation("order {OrderId} saved", msg.Message.Key);
        }
        catch (Exception e)
        {
            log.LogError(e, "order {OrderId} failed", msg.Message.Key);
        }
    }
}) { IsBackground = true, Name = "consumer" };
worker.Start();
app.Urls.Add("http://0.0.0.0:8080");
app.Run();
