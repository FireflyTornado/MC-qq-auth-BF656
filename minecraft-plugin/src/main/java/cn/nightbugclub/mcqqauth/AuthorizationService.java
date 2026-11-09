package cn.nightbugclub.mcqqauth;

import org.eclipse.jetty.client.CompletableResponseListener;
import org.eclipse.jetty.client.ContentResponse;
import org.eclipse.jetty.client.HttpClient;
import org.eclipse.jetty.client.ProxyAuthenticationProtocolHandler;
import org.eclipse.jetty.client.Request;
import org.eclipse.jetty.client.WWWAuthenticationProtocolHandler;
import org.eclipse.jetty.client.transport.HttpClientTransportOverHTTP;
import org.eclipse.jetty.http.HttpHeader;
import org.eclipse.jetty.util.component.LifeCycle;
import org.eclipse.jetty.util.ssl.SslContextFactory;
import org.eclipse.jetty.util.thread.QueuedThreadPool;
import org.eclipse.jetty.util.thread.ScheduledExecutorScheduler;
import org.eclipse.jetty.websocket.api.Callback;
import org.eclipse.jetty.websocket.api.Session;
import org.eclipse.jetty.websocket.client.ClientUpgradeRequest;
import org.eclipse.jetty.websocket.client.WebSocketClient;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.time.Duration;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ScheduledThreadPoolExecutor;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.logging.Logger;

import static java.util.concurrent.TimeUnit.MILLISECONDS;

public final class AuthorizationService implements AutoCloseable {
    private static final int MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024;
    private static final int PING_INTERVAL_MS = 15_000;
    private static final int PONG_TIMEOUT_MS = 10_000;

    enum PlayerStatus { AUTHORIZED, UNAUTHORIZED, UNKNOWN }

    record View(AuthProtocol.Snapshot snapshot, boolean ready) {
        static final View OFFLINE = new View(null, false);

        PlayerStatus status(String playerName) {
            if (!ready) {
                return PlayerStatus.UNKNOWN;
            }
            return snapshot.allows(playerName) ? PlayerStatus.AUTHORIZED : PlayerStatus.UNAUTHORIZED;
        }
    }

    private record Published(View view, Connection source) {}

    private final PluginSettings settings;
    private final SslContextFactory.Client tls;
    private final Logger logger;
    private final Runnable onSynchronized;
    private final ScheduledThreadPoolExecutor worker;
    private final AtomicBoolean closed = new AtomicBoolean();
    private volatile Published published = new Published(View.OFFLINE, null);

    // Network callbacks enqueue work here. Only the worker mutates the session and refresh state.
    private HttpClient http;
    private WebSocketClient websocket;
    private Connection connection;
    private long wantedVersion = -1;
    private int reconnectDelay;

    AuthorizationService(PluginSettings settings, SslContextFactory.Client tls, Logger logger,
                         Runnable onSynchronized) {
        this.settings = settings;
        this.tls = tls;
        this.logger = logger;
        this.onSynchronized = onSynchronized;
        reconnectDelay = settings.reconnectMinMs;
        worker = new ScheduledThreadPoolExecutor(1, task -> {
            Thread thread = new Thread(task, "MCQQAuth-sync");
            thread.setDaemon(true);
            return thread;
        });
        worker.setRemoveOnCancelPolicy(true);
        worker.setExecuteExistingDelayedTasksAfterShutdownPolicy(false);
    }

    View view() {
        if (closed.get()) {
            return View.OFFLINE;
        }
        Published current = published;
        Connection source = current.source();
        if (source != null && (source.disconnected
                || source.latestNoticeVersion > current.view().snapshot().version())) {
            return new View(current.view().snapshot(), false);
        }
        return current.view();
    }

    void start() {
        dispatch(() -> {
            try {
                QueuedThreadPool io = new QueuedThreadPool(12, 2);
                io.setName("MCQQAuth-io");
                io.setDaemon(true);
                io.setStopTimeout(1_000);
                tls.setEndpointIdentificationAlgorithm("HTTPS");
                http = new HttpClient(new HttpClientTransportOverHTTP(1));
                http.setExecutor(io);
                http.setScheduler(new ScheduledExecutorScheduler("MCQQAuth-timer", true));
                http.setSslContextFactory(tls);
                http.setConnectTimeout(settings.connectTimeoutMs);
                http.setAddressResolutionTimeout(settings.connectTimeoutMs);
                http.setFollowRedirects(false);
                http.setMaxConnectionsPerDestination(2);
                websocket = new WebSocketClient(http);
                websocket.setMaxTextMessageSize(4096);
                websocket.setMaxBinaryMessageSize(4096);
                websocket.setIdleTimeout(Duration.ofSeconds(40));
                websocket.setStopTimeout(1_000);
                websocket.start();
                // Bearer is sent explicitly. Return auth failures as ordinary HTTP responses.
                http.getProtocolHandlers().remove(WWWAuthenticationProtocolHandler.NAME);
                http.getProtocolHandlers().remove(ProxyAuthenticationProtocolHandler.NAME);
            } catch (Exception e) {
                logger.severe("授权客户端初始化失败（" + e.getClass().getSimpleName() + "）");
                close();
                return;
            }
            connect();
        });
    }

    private void connect() {
        if (closed.get()) {
            return;
        }
        Connection attempt = new Connection();
        connection = attempt;
        ClientUpgradeRequest upgrade = new ClientUpgradeRequest();
        upgrade.setHeader(HttpHeader.AUTHORIZATION.asString(), "Bearer " + settings.token);
        upgrade.setTimeout(settings.requestTimeoutMs, MILLISECONDS);
        try {
            attempt.upgrade = websocket.connect(attempt, settings.websocketUrl, upgrade);
        } catch (IOException | RuntimeException e) {
            fail(attempt, "WSS 握手无法启动（" + failureType(e) + "）");
            return;
        }
        attempt.upgrade.whenComplete((session, failure) -> {
            if (failure != null) {
                dispatch(() -> fail(attempt, "WSS 握手失败（" + failureType(failure) + "）"));
            }
        });
    }

    private void receive(Connection source, AuthProtocol.Notice notice) {
        if (connection != source) {
            return;
        }
        switch (notice.type()) {
            case CONNECTED -> {
                if (!source.confirmed) {
                    source.confirmed = true;
                    source.timer.cancel(false);
                    schedulePing(source);
                    refresh(source);
                }
            }
            case AUTH_UPDATED -> {
                if (notice.version() > wantedVersion) {
                    wantedVersion = notice.version();
                    markUnready();
                    refresh(source);
                }
            }
        }
        source.demand();
    }

    private void refresh(Connection source) {
        if (!source.confirmed || source.disconnected || source.snapshotRequest != null || closed.get()) {
            return;
        }
        long requestedVersion = wantedVersion;
        Request request = http.newRequest(settings.snapshotUrl)
                .headers(headers -> headers
                        .put(HttpHeader.AUTHORIZATION, "Bearer " + settings.token)
                        .put(HttpHeader.ACCEPT, "application/json"))
                .timeout(settings.requestTimeoutMs, MILLISECONDS);
        source.snapshotRequest = request;
        new CompletableResponseListener(request, MAX_SNAPSHOT_BYTES).send()
                .whenComplete((response, failure) ->
                        dispatch(() -> finishRefresh(source, requestedVersion, response, failure)));
    }

    private void finishRefresh(Connection source, long requestedVersion,
                               ContentResponse response, Throwable failure) {
        if (connection != source || source.disconnected) {
            return;
        }
        source.snapshotRequest = null;
        if (failure != null) {
            fail(source, "HTTPS 快照请求失败（" + failureType(failure) + "）");
            return;
        }
        if (response.getStatus() < 200 || response.getStatus() >= 300) {
            fail(source, "HTTPS 快照返回 HTTP " + response.getStatus());
            return;
        }
        AuthProtocol.Snapshot candidate;
        try {
            candidate = AuthProtocol.snapshot(response.getContent());
        } catch (IOException e) {
            fail(source, "HTTPS 快照格式无效");
            return;
        }
        long minimumVersion = Math.max(wantedVersion, source.latestNoticeVersion);
        if (candidate.version() < minimumVersion) {
            if (candidate.version() >= requestedVersion && minimumVersion > requestedVersion) {
                // A newer notice arrived during this request. Fetch again without publishing this list.
                wantedVersion = minimumVersion;
                markUnready();
                refresh(source);
            } else {
                fail(source, "HTTPS 快照版本落后，等待重新同步");
            }
            return;
        }
        AuthProtocol.Snapshot previous = published.view().snapshot();
        if (previous != null && candidate.version() == previous.version()
                && !candidate.activeNames().equals(previous.activeNames())) {
            fail(source, "HTTPS 快照同一版本返回不同名单");
            return;
        }
        wantedVersion = candidate.version();
        published = new Published(new View(candidate, true), source);
        reconnectDelay = settings.reconnectMinMs;
        onSynchronized.run();
    }

    private void schedulePing(Connection source) {
        source.timer = worker.schedule(() -> {
            if (closed.get() || source.disconnected) {
                return;
            }
            long ping = ++source.ping;
            source.awaitingPong = true;
            source.timer = worker.schedule(() -> fail(source, "WSS 心跳超时"), PONG_TIMEOUT_MS, MILLISECONDS);
            source.session.sendPing(ByteBuffer.allocate(Long.BYTES).putLong(ping).flip(),
                    Callback.from(() -> {}, failure -> source.reportFailure("WSS 心跳发送失败")));
        }, PING_INTERVAL_MS, MILLISECONDS);
    }

    private void fail(Connection source, String reason) {
        if (connection != source || closed.get()) {
            return;
        }
        wantedVersion = Math.max(wantedVersion, source.latestNoticeVersion);
        markUnready();
        connection = null;
        source.cancelRequests();
        if (source.session != null) {
            source.session.disconnect();
        }
        logger.warning(reason + "，" + reconnectDelay + " 毫秒后重连");
        worker.schedule(this::connect, reconnectDelay, MILLISECONDS);
        reconnectDelay = Math.min(reconnectDelay * 2, settings.reconnectMaxMs);
    }

    private void markUnready() {
        published = new Published(new View(published.view().snapshot(), false), null);
    }

    private boolean dispatch(Runnable action) {
        if (closed.get()) {
            return false;
        }
        try {
            worker.execute(() -> {
                if (!closed.get()) {
                    action.run();
                }
            });
            return true;
        } catch (RejectedExecutionException e) {
            return false;
        }
    }

    @Override
    public void close() {
        if (!closed.compareAndSet(false, true)) {
            return;
        }
        worker.execute(this::stop);
    }

    private void stop() {
        try {
            if (connection != null) {
                connection.cancelRequests();
            }
        } catch (RuntimeException e) {
            logger.warning("授权请求取消失败（" + failureType(e) + "）");
        } finally {
            connection = null;
            published = new Published(View.OFFLINE, null);
            stopClient(websocket, "WSS");
            stopClient(http, "HTTPS");
            worker.shutdown();
        }
    }

    private void stopClient(LifeCycle client, String label) {
        if (client != null && !client.isStopped()) {
            try {
                client.stop();
            } catch (Exception e) {
                logger.warning(label + " 客户端关闭失败（" + failureType(e) + "）");
            }
        }
    }

    boolean awaitStopped(Duration timeout) throws InterruptedException {
        return worker.awaitTermination(timeout.toMillis(), MILLISECONDS);
    }

    private static String failureType(Throwable failure) {
        if (failure instanceof CompletionException && failure.getCause() != null) {
            return failure.getCause().getClass().getSimpleName();
        }
        return failure.getClass().getSimpleName();
    }

    // Jetty resolves listener methods via public method handles.
    public final class Connection implements Session.Listener {
        // These observations invalidate a published view even while its worker is processing HTTPS.
        private volatile boolean disconnected;
        private volatile long latestNoticeVersion = -1;
        private CompletableFuture<Session> upgrade;
        private Session session;
        private ScheduledFuture<?> timer;
        private Request snapshotRequest;
        private boolean confirmed;
        private long ping;
        private boolean awaitingPong;

        @Override
        public void onWebSocketOpen(Session opened) {
            if (!dispatch(() -> {
                if (connection != this) {
                    opened.disconnect();
                    return;
                }
                session = opened;
                timer = worker.schedule(() -> fail(this, "WSS 未发送连接确认"),
                        settings.requestTimeoutMs, MILLISECONDS);
                demand();
            })) {
                opened.disconnect();
            }
        }

        @Override
        public void onWebSocketText(String message) {
            try {
                AuthProtocol.Notice notice = AuthProtocol.notice(message);
                if (notice.type() == AuthProtocol.MessageType.AUTH_UPDATED) {
                    latestNoticeVersion = Math.max(latestNoticeVersion, notice.version());
                }
                dispatch(() -> receive(this, notice));
            } catch (IOException e) {
                reportFailure("WSS 通知格式无效");
            }
        }

        @Override
        public void onWebSocketBinary(ByteBuffer payload, Callback callback) {
            callback.succeed();
            reportFailure("WSS 收到意外的二进制消息");
        }

        @Override
        public void onWebSocketPing(ByteBuffer payload) {
            dispatch(this::demand);
        }

        @Override
        public void onWebSocketPong(ByteBuffer payload) {
            long pong = payload.remaining() == Long.BYTES ? payload.getLong() : -1;
            dispatch(() -> {
                if (connection == this && awaitingPong && pong == ping) {
                    awaitingPong = false;
                    timer.cancel(false);
                    schedulePing(this);
                }
                demand();
            });
        }

        @Override
        public void onWebSocketClose(int statusCode, String reason) {
            reportFailure("WSS 已断开（" + statusCode + "）");
        }

        @Override
        public void onWebSocketError(Throwable failure) {
            reportFailure("WSS 连接错误（" + failureType(failure) + "）");
        }

        private void reportFailure(String reason) {
            disconnected = true;
            dispatch(() -> fail(this, reason));
        }

        private void demand() {
            if (connection == this) {
                session.demand();
            }
        }

        private void cancelRequests() {
            if (timer != null) {
                timer.cancel(false);
            }
            if (upgrade != null) {
                upgrade.cancel(true);
            }
            if (snapshotRequest != null) {
                snapshotRequest.abort(new IOException("授权连接已结束"));
                snapshotRequest = null;
            }
        }
    }
}
