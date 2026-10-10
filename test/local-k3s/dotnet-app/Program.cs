// 指標案例驗證用的真的 .NET 應用（test/local-k3s/dotnet-app.yaml）：ASP.NET Core 9 訂單 API，
// 每個請求從 Npgsql 連線池拿連線查 PostgreSQL。不改程式碼，掛 OpenTelemetry .NET 自動埋點（跟 Java agent 一樣的做法），
// 確認 .NET 的 HTTP、runtime（GC、執行緒池）、連線池指標名稱，以及 log 的 trace_id。
// 開關（Pod 裡 /ctl 底下的檔案，app 和 load 兩個容器共用）：
//   /ctl/leak      記憶體洩漏：每個請求往不會清的 list 放 N KB（N 寫在檔案裡，預設 12）
//   /ctl/leakconn  連線洩漏：拿了連線不還
//   /ctl/starve    執行緒池飢餓：每個請求同步等一個非同步工作（Task.Delay(2000).Wait()），把執行緒池的執行緒卡住
using Npgsql;

var builder = WebApplication.CreateBuilder(args);
builder.Logging.ClearProviders();
builder.Logging.AddJsonConsole(o =>
{
    o.IncludeScopes = true;               // trace_id、span_id 在 scope 裡
    o.UseUtcTimestamp = true;
    o.TimestampFormat = "yyyy-MM-ddTHH:mm:ss.fffZ";
});
var app = builder.Build();

var ds = NpgsqlDataSource.Create(
    "Host=localhost;Username=postgres;Password=test-only;Database=postgres;Maximum Pool Size=5;Timeout=2");
for (var i = 0; ; i++)
{
    try
    {
        await using var c = await ds.OpenConnectionAsync();
        await using var cmd = new NpgsqlCommand(
            "create table if not exists lots(id int primary key, state text);" +
            "insert into lots select g, 'RUN' from generate_series(0, 999) g on conflict do nothing", c);
        await cmd.ExecuteNonQueryAsync();
        break;
    }
    catch when (i < 30) { await Task.Delay(1000); }   // 等 PostgreSQL 起來
}

var cache = new List<byte[]>();
var leaked = new List<NpgsqlConnection>();

app.MapGet("/orders/{id:int}", async (int id, ILogger<Program> log) =>
{
    if (File.Exists("/ctl/leak"))
    {
        var kb = int.TryParse(File.ReadAllText("/ctl/leak").Trim(), out var n) ? n : 12;
        lock (cache) cache.Add(new byte[kb * 1024]);
    }
    if (File.Exists("/ctl/starve"))
        Task.Delay(2000).Wait();          // 同步等非同步：佔住執行緒池的執行緒 2 秒
    var conn = await ds.OpenConnectionAsync();
    await using (var cmd = new NpgsqlCommand("select state from lots where id = $1", conn))
    {
        cmd.Parameters.AddWithValue(id % 1000);
        await cmd.ExecuteScalarAsync();
    }
    if (File.Exists("/ctl/leakconn")) lock (leaked) leaked.Add(conn);
    else await conn.DisposeAsync();
    log.LogInformation("order {OrderId} handled", id);
    return Results.Ok(new { id });
});

app.Run("http://0.0.0.0:8080");
