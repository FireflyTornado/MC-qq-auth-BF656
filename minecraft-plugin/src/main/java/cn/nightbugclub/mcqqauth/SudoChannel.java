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
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.function.Consumer;
import java.util.logging.Logger;

import static java.util.concurrent.TimeUnit.SECONDS;

final class SudoChannel implements AutoCloseable {
    private static final int MAX_MESSAGE = 8192;
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

    private final Settings settings;
    private final SudoCommands commands;
    private final Consumer<Runnable> onMain;
    private final Logger log;
    private final ScheduledExecutorService timer = Executors.newSingleThreadScheduledExecutor(task -> {
        Thread thread = new Thread(task, "MCQQAuth-sudo");
        thread.setDaemon(true);
        return thread;
    });
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5))
            .proxy(ProxySelector.of(null)).followRedirects(HttpClient.Redirect.NEVER).build();

    // Guard the connection and execution slot across both the network and server threads.
    private boolean closed;
    private Connection active;
    private Request running;
    private final LinkedHashMap<String, Long> recent = new LinkedHashMap<>();

    SudoChannel(Settings settings, SudoCommands commands, Consumer<Runnable> onMain, Logger log) {
        this.settings = settings;
        this.commands = commands;
        this.onMain = onMain;
        this.log = log;
    }

    void start() {
        timer.scheduleWithFixedDelay(this::maintain, 0, 5, SECONDS);
    }

    private synchronized void maintain() {
        if (closed) {
            return;
        }
        if (active == null) {
            Connection attempt = new Connection();
            active = attempt;
            http.newWebSocketBuilder().connectTimeout(Duration.ofSeconds(5))
                    .header("Authorization", "Bearer " + settings.token())
                    .buildAsync(settings.url(), attempt)
                    .whenComplete((socket, failure) -> {
                        if (failure != null) {
                            disconnect(attempt, "握手失败");
                        }
                    });
        } else if (active.socket != null) {
            Connection connection = active;
            if (System.nanoTime() - connection.lastPong > SECONDS.toNanos(15)) {
                disconnect(connection, "心跳超时");
            } else {
                connection.socket.sendPing(ByteBuffer.allocate(0)).whenComplete((socket, failure) -> {
                    if (failure != null) {
                        disconnect(connection, "心跳发送失败");
                    }
                });
            }
        }
    }

    private synchronized void disconnect(Connection connection, String reason) {
        if (active != connection) {
            return;
        }
        active = null;
        if (connection.socket != null) {
            connection.socket.abort();
        }
        log.warning("/sudo 通道断开：" + reason);
    }

    private void receive(Connection source, Request request) {
        String rejection;
        synchronized (this) {
            if (active != source) {
                return;
            }
            long now = System.currentTimeMillis();
            recent.values().removeIf(deadline -> deadline <= now);
            // Reserve a clock-skew margin at execution time so a slow clock cannot start work late.
            if (request.deadlineMs <= now + CLOCK_SKEW_MS || request.deadlineMs - now > TTL_MS + CLOCK_SKEW_MS) {
                rejection = "expired";
            } else if (running != null || recent.size() >= 1024) {
                rejection = "busy";
            } else if (recent.putIfAbsent(request.id, request.deadlineMs) != null) {
                rejection = "rejected";
            } else {
                running = request;
                rejection = null;
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
            canRun = active == source && running == request
                    && System.currentTimeMillis() + CLOCK_SKEW_MS < request.deadlineMs;
        }
        SudoCommands.Result result = new SudoCommands.Result("expired");
        try {
            if (canRun) {
                result = commands.execute(request.command);
            }
        } catch (RuntimeException e) {
            result = new SudoCommands.Result("error");
            log.warning("/sudo #" + request.id + " 执行异常（" + e.getClass().getSimpleName() + "）");
        } finally {
            finish(source, request, result);
        }
    }

    private synchronized void finish(Connection source, Request request, SudoCommands.Result result) {
        if (running == request) {
            running = null;
            log.info("/sudo #" + request.id + " result=" + result.status());
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
        if (active != null) {
            WebSocket socket = active.socket;
            active = null;
            if (socket != null) {
                socket.abort();
            }
        }
    }

    private final class Connection implements WebSocket.Listener {
        private final StringBuilder message = new StringBuilder();
        private WebSocket socket;
        private volatile long lastPong;

        @Override
        public void onOpen(WebSocket opened) {
            synchronized (SudoChannel.this) {
                if (active != this) {
                    opened.abort();
                    return;
                }
                socket = opened;
                lastPong = System.nanoTime();
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
            lastPong = System.nanoTime();
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
