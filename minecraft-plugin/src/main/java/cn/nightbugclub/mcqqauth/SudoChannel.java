package cn.nightbugclub.mcqqauth;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import org.bukkit.configuration.ConfigurationSection;

import java.net.ProxySelector;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.nio.ByteBuffer;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.CancellationException;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.function.Consumer;
import java.util.logging.Logger;

import static java.util.concurrent.TimeUnit.MILLISECONDS;
import static java.util.concurrent.TimeUnit.SECONDS;

final class SudoChannel implements AutoCloseable {
    private static final int MAX_MESSAGE = 8192;
    private static final int PING_INTERVAL_SECONDS = 20;
    private static final int PONG_TIMEOUT_SECONDS = 15;
    private static final long TTL_MS = 30_000;
    private static final long CLOCK_SKEW_MS = 1_000;
    private static final Gson JSON = new Gson();

    record Settings(URI url, String token) {
        static Settings read(ConfigurationSection config, String authToken) {
            Object enabled = config.get("sudo.enabled");
            if (Boolean.FALSE.equals(enabled)) {
                return null;
            }
            if (!Boolean.TRUE.equals(enabled)) {
                throw new IllegalArgumentException("sudo.enabled 必须为布尔值");
            }
            String token = config.getString("sudo.token", "");
            if (!config.isString("sudo.token") || token.length() > 4096 || !token.matches("[A-Za-z0-9._~+/-]+=*")
                    || token.equals(authToken) || token.equals("REPLACE_WITH_MC_SUDO_PLUGIN_TOKEN")) {
                throw new IllegalArgumentException("sudo.token 必须为独立的有效凭据");
            }
            URI url;
            try {
                url = URI.create(config.getString("sudo.websocket-url", ""));
            } catch (IllegalArgumentException e) {
                throw new IllegalArgumentException("sudo.websocket-url 无效");
            }
            if (!"wss".equals(url.getScheme()) || url.getHost() == null || url.getPort() == 0
                    || url.getPort() > 65535 || url.getUserInfo() != null || url.getRawQuery() != null
                    || url.getRawFragment() != null || url.getRawPath().isEmpty()) {
                throw new IllegalArgumentException("sudo.websocket-url 必须为 WSS 地址");
            }
            return new Settings(url, token);
        }
    }

    record Request(String id, String command, long deadlineMs) {
        static Request parse(String raw) {
            JsonObject json = JsonParser.parseString(raw).getAsJsonObject();
            var id = json.getAsJsonPrimitive("id");
            var command = json.getAsJsonPrimitive("command");
            var deadline = json.getAsJsonPrimitive("deadline_ms");
            if (!id.isString() || !id.getAsString().matches("[0-9a-f]{32}") || !command.isString()
                    || !deadline.isNumber() || !deadline.getAsString().matches("[0-9]{1,15}")) {
                throw new IllegalArgumentException("Invalid sudo request");
            }
            return new Request(id.getAsString(), command.getAsString(), deadline.getAsLong());
        }
    }

    // Accessed under the channel lock after validating the request deadline.
    static final class RequestHistory {
        private static final int MAX_RECENT = 1024;
        private final LinkedHashMap<String, Long> recent = new LinkedHashMap<>();
        private long blockedUntil;

        String admit(Request request, long now, boolean running) {
            recent.values().removeIf(deadline -> deadline <= now);
            if (recent.containsKey(request.id)) {
                return "rejected";
            }
            if (recent.size() >= MAX_RECENT || now < blockedUntil) {
                // Keep refusing untracked requests until every refused request has expired.
                blockedUntil = Math.max(blockedUntil, request.deadlineMs);
                return "busy";
            }
            recent.put(request.id, request.deadlineMs);
            return running ? "busy" : null;
        }
    }

    private final Settings settings;
    private final SudoCommands commands;
    private final Consumer<Runnable> onMain;
    private final Logger log;
    private final ScheduledExecutorService timer;
    private final HttpClient http;
    private final ReconnectBackoff reconnect;

    // Guard the connection and execution slot across both the network and server threads.
    private boolean closed;
    private Connection active;
    private Request running;
    private boolean executing;
    private CompletableFuture<SudoCommands.Result> collecting;
    private final RequestHistory history = new RequestHistory();

    SudoChannel(Settings settings, SudoCommands commands, Consumer<Runnable> onMain, Logger log,
                int reconnectMinMs, int reconnectMaxMs) {
        this(settings, commands, onMain, log, reconnectMinMs, reconnectMaxMs,
                HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5))
                        .proxy(ProxySelector.of(null)).followRedirects(HttpClient.Redirect.NEVER).build(),
                createTimer());
    }

    SudoChannel(Settings settings, SudoCommands commands, Consumer<Runnable> onMain, Logger log,
                int reconnectMinMs, int reconnectMaxMs, HttpClient http, ScheduledExecutorService timer) {
        this.settings = settings;
        this.commands = commands;
        this.onMain = onMain;
        this.log = log;
        this.http = http;
        this.timer = timer;
        this.reconnect = new ReconnectBackoff(reconnectMinMs, reconnectMaxMs);
    }

    private static ScheduledExecutorService createTimer() {
        var executor = new ScheduledThreadPoolExecutor(1, task -> {
            Thread thread = new Thread(task, "MCQQAuth-sudo");
            thread.setDaemon(true);
            return thread;
        });
        executor.setRemoveOnCancelPolicy(true);
        return executor;
    }

    void start() {
        timer.schedule(this::connect, 0, MILLISECONDS);
    }

    private synchronized void connect() {
        if (closed || active != null) {
            return;
        }
        Connection attempt = new Connection();
        active = attempt;
        try {
            http.newWebSocketBuilder().connectTimeout(Duration.ofSeconds(5))
                    .header("Authorization", "Bearer " + settings.token())
                    .buildAsync(settings.url(), attempt)
                    .whenComplete((socket, failure) -> {
                        if (failure != null) {
                            disconnect(attempt, "握手失败");
                        }
                    });
        } catch (RuntimeException e) {
            disconnect(attempt, "握手失败");
        }
    }

    private void schedulePing(Connection connection) {
        connection.heartbeat = timer.schedule(() -> sendPing(connection), PING_INTERVAL_SECONDS, SECONDS);
    }

    private synchronized void sendPing(Connection connection) {
        if (active != connection) {
            return;
        }
        long ping = ++connection.ping;
        connection.awaitingPong = true;
        connection.heartbeat = timer.schedule(() -> pongTimedOut(connection, ping), PONG_TIMEOUT_SECONDS, SECONDS);
        connection.socket.sendPing(ByteBuffer.allocate(Long.BYTES).putLong(ping).flip())
                .whenComplete((socket, failure) -> {
                    if (failure != null) {
                        disconnect(connection, "心跳发送失败");
                    }
                });
    }

    private synchronized void pongTimedOut(Connection connection, long ping) {
        if (connection.awaitingPong && connection.ping == ping) {
            disconnect(connection, "心跳超时");
        }
    }

    private synchronized void disconnect(Connection connection, String reason) {
        if (active != connection) {
            return;
        }
        active = null;
        if (!executing) {
            cancelCollection();
        }
        connection.abort();
        int delay = reconnect.nextDelayMs();
        log.warning("/sudo 通道断开：" + reason + "，" + delay + " 毫秒后重连");
        timer.schedule(this::connect, delay, MILLISECONDS);
    }

    private void receive(Connection source, Request request) {
        String rejection;
        synchronized (this) {
            if (active != source) {
                return;
            }
            long now = System.currentTimeMillis();
            // Reserve a clock-skew margin at execution time so a slow clock cannot start work late.
            if (request.deadlineMs <= now + CLOCK_SKEW_MS || request.deadlineMs - now > TTL_MS + CLOCK_SKEW_MS) {
                rejection = "expired";
            } else {
                rejection = history.admit(request, now, running != null);
                if (rejection == null) {
                    running = request;
                }
            }
        }
        if (rejection != null) {
            reply(source, request, new SudoCommands.Result(rejection));
            return;
        }
        try {
            onMain.accept(() -> execute(source, request));
        } catch (RuntimeException e) {
            finish(source, request, new SudoCommands.Result("error"));
        }
    }

    private void execute(Connection source, Request request) {
        boolean canRun;
        synchronized (this) {
            if (active != source || running != request) {
                return;
            }
            canRun = System.currentTimeMillis() + CLOCK_SKEW_MS < request.deadlineMs;
            // Claim execution under the same lock used to invalidate queued work on disconnect.
            executing = canRun;
        }
        if (!canRun) {
            finish(source, request, new SudoCommands.Result("expired"));
            return;
        }
        try {
            var result = commands.execute(request.command, request.deadlineMs - CLOCK_SKEW_MS);
            synchronized (this) {
                executing = false;
                if (active != source) {
                    running = null;
                    result.cancel(false);
                    return;
                }
                collecting = result;
            }
            result.whenComplete((response, failure) -> finishExecution(source, request, response, failure));
        } catch (RuntimeException e) {
            finishExecution(source, request, null, e);
        }
    }

    private void finishExecution(Connection source, Request request, SudoCommands.Result result, Throwable failure) {
        if (failure instanceof CancellationException) {
            return;
        }
        if (failure != null) {
            log.warning("/sudo #" + request.id + " 执行异常（" + failure.getClass().getSimpleName() + "）");
            result = new SudoCommands.Result("error");
        }
        finish(source, request, result);
    }

    private synchronized void finish(Connection source, Request request, SudoCommands.Result result) {
        if (running == request) {
            running = null;
            executing = false;
            collecting = null;
            log.info("/sudo #" + request.id + " result=" + result.status()
                    + (result.captureIncomplete() ? " capture=incomplete" : ""));
            reply(source, request, result);
        }
    }

    private synchronized void reply(Connection source, Request request, SudoCommands.Result result) {
        if (active != source) {
            return;
        }
        JsonObject response = new JsonObject();
        response.addProperty("id", request.id);
        response.addProperty("status", result.status());
        response.addProperty("output", result.output());
        if (result.truncated()) {
            response.addProperty("truncated", true);
        }
        if (result.captureIncomplete()) {
            response.addProperty("capture_incomplete", true);
        }
        source.socket.sendText(JSON.toJson(response), true).whenComplete((socket, failure) -> {
            if (failure != null) {
                disconnect(source, "回执发送失败");
            }
        });
    }

    @Override
    public synchronized void close() {
        closed = true;
        timer.shutdownNow();
        if (!executing) {
            cancelCollection();
        }
        if (active != null) {
            Connection connection = active;
            active = null;
            connection.abort();
        }
    }

    private void cancelCollection() {
        running = null;
        if (collecting != null) {
            var task = collecting;
            collecting = null;
            task.cancel(false);
        }
    }

    private final class Connection implements WebSocket.Listener {
        private final StringBuilder message = new StringBuilder();
        private WebSocket socket;
        private ScheduledFuture<?> heartbeat;
        private long ping;
        private boolean awaitingPong;

        private void abort() {
            if (heartbeat != null) {
                heartbeat.cancel(false);
            }
            if (socket != null) {
                socket.abort();
            }
        }

        @Override
        public void onOpen(WebSocket opened) {
            synchronized (SudoChannel.this) {
                if (active != this) {
                    opened.abort();
                    return;
                }
                socket = opened;
                reconnect.reset();
                schedulePing(this);
            }
            opened.request(1);
        }

        @Override
        public CompletionStage<?> onText(WebSocket socket, CharSequence data, boolean last) {
            if (data.length() > MAX_MESSAGE - message.length()) {
                disconnect(this, "消息过长");
                return null;
            }
            message.append(data);
            if (last) {
                Request request;
                try {
                    request = Request.parse(message.toString());
                } catch (RuntimeException e) {
                    disconnect(this, "消息格式错误");
                    return null;
                }
                message.setLength(0);
                receive(this, request);
            }
            socket.request(1);
            return null;
        }

        @Override
        public CompletionStage<?> onPong(WebSocket socket, ByteBuffer data) {
            synchronized (SudoChannel.this) {
                if (active == this && awaitingPong && data.remaining() == Long.BYTES && data.getLong() == ping) {
                    awaitingPong = false;
                    heartbeat.cancel(false);
                    schedulePing(this);
                }
            }
            socket.request(1);
            return null;
        }

        @Override
        public CompletionStage<?> onBinary(WebSocket socket, ByteBuffer data, boolean last) {
            disconnect(this, "不支持二进制消息");
            return null;
        }

        @Override
        public CompletionStage<?> onClose(WebSocket socket, int statusCode, String reason) {
            disconnect(this, "连接已关闭");
            return null;
        }

        @Override
        public void onError(WebSocket socket, Throwable failure) {
            disconnect(this, "连接错误");
        }
    }
}
