package cn.nightbugclub.mcqqauth;

import org.bukkit.command.CommandSender;
import org.bukkit.command.PluginCommandYamlParser;
import org.bukkit.plugin.PluginDescriptionFile;
import org.bukkit.plugin.PluginManager;
import org.bukkit.scheduler.BukkitScheduler;
import org.bukkit.scheduler.BukkitTask;
import org.eclipse.jetty.http.HttpHeader;
import org.eclipse.jetty.server.Handler;
import org.eclipse.jetty.server.HttpConfiguration;
import org.eclipse.jetty.server.HttpConnectionFactory;
import org.eclipse.jetty.server.Request;
import org.eclipse.jetty.server.Response;
import org.eclipse.jetty.server.SecureRequestCustomizer;
import org.eclipse.jetty.server.Server;
import org.eclipse.jetty.server.ServerConnector;
import org.eclipse.jetty.server.SslConnectionFactory;
import org.eclipse.jetty.util.ssl.SslContextFactory;
import org.eclipse.jetty.websocket.api.Callback;
import org.eclipse.jetty.websocket.api.Session;
import org.eclipse.jetty.websocket.server.ServerWebSocketContainer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.Timeout;
import org.junit.jupiter.api.io.TempDir;
import org.junit.jupiter.api.parallel.ResourceLock;
import org.junit.jupiter.api.parallel.Resources;

import java.net.URI;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.BooleanSupplier;
import java.util.logging.LogRecord;
import java.util.logging.Logger;

import static java.util.concurrent.TimeUnit.MILLISECONDS;
import static java.util.concurrent.TimeUnit.SECONDS;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

@Timeout(20)
public class AuthorizationServiceTest {
    private static final String TOKEN = "test-only.api-token";
    private static final String PASSWORD = "test-keystore";
    private static final long VERSION = 1_794_141_600_123L;
    @TempDir
    static Path tempDir;
    private static Path keyStore;

    private LocalApi api;
    private AuthorizationService service;
    private final BlockingQueue<AuthorizationService.View> updates = new LinkedBlockingQueue<>();
    private final BlockingQueue<String> logs = new LinkedBlockingQueue<>();

    @BeforeAll
    static void generateLocalCertificate() throws Exception {
        keyStore = tempDir.resolve("local.p12");
        String keytool = Path.of(System.getProperty("java.home"), "bin", "keytool").toString();
        Process process = new ProcessBuilder(keytool, "-genkeypair", "-alias", "local", "-keyalg", "RSA",
                "-dname", "CN=localhost", "-ext", "SAN=dns:localhost", "-validity", "2",
                "-storetype", "PKCS12", "-keystore", keyStore.toString(),
                "-storepass", PASSWORD, "-keypass", PASSWORD).redirectErrorStream(true).start();
        assertTrue(process.waitFor(10, SECONDS), "keytool timed out");
        assertEquals(0, process.exitValue(), new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8));
    }

    @BeforeEach
    void startApi() throws Exception {
        api = new LocalApi();
    }

    @AfterEach
    void stop() throws Exception {
        try {
            if (service != null) {
                service.close();
                assertTrue(service.awaitStopped(Duration.ofSeconds(5)));
            }
        } finally {
            api.close();
        }
    }

    @Test
    void firstSnapshotUsesBothBearerEndpointsAndIsImmutable() throws Exception {
        start(TOKEN, 3_000);
        assertFalse(service.view().ready());
        take(api.sessions);
        Exchange initial = take(api.requests);
        assertFalse(service.view().ready());
        initial.reply(200, snapshot(0, "\"steve\",\"alex_1\""));
        AuthorizationService.View view = take(updates);
        assertTrue(view.ready());
        assertEquals(0, view.snapshot().version());
        assertTrue(view.snapshot().allows("StEvE"));
        assertFalse(view.snapshot().allows("NotBound"));
        assertFalse(view.snapshot().allows("Steve "));
        assertThrows(UnsupportedOperationException.class, () -> view.snapshot().activeNames().clear());
        assertNull(api.requests.poll(200, MILLISECONDS));
    }

    @Test
    void respondsToServerPingAndContinuesReceivingNotices() throws Exception {
        start(TOKEN, 3_000);
        Session session = take(api.sessions);
        take(api.requests).reply(200, snapshot(VERSION, ""));
        take(updates);

        assertPong(session);

        notice(session, VERSION + 1);
        take(api.requests).reply(200, snapshot(VERSION + 1, ""));
        assertEquals(VERSION + 1, take(updates).snapshot().version());
    }

    @Test
    @ResourceLock(Resources.SYSTEM_PROPERTIES)
    void pluginModeCommandsKeepTheSameLiveWssSessionAndConfirmedSnapshot(@TempDir Path pluginData) throws Exception {
        Files.writeString(pluginData.resolve("config.yml"), """
                mode: OBSERVE
                api:
                  token: %s
                  snapshot-url: https://localhost:%d/internal/mc/snapshot
                  websocket-url: wss://localhost:%d/internal/mc/ws
                """.formatted(TOKEN, api.port(), api.port()));
        MCQQAuthPlugin plugin = mock(MCQQAuthPlugin.class, CALLS_REAL_METHODS);
        var server = mock(org.bukkit.Server.class);
        var scheduler = mock(BukkitScheduler.class);
        var mainTasks = new LinkedBlockingQueue<Runnable>();
        when(server.getPluginManager()).thenReturn(mock(PluginManager.class));
        when(server.isPrimaryThread()).thenReturn(true);
        doReturn(List.of()).when(server).getOnlinePlayers();
        when(server.getScheduler()).thenReturn(scheduler);
        when(scheduler.runTask(eq(plugin), any(Runnable.class))).thenAnswer(call -> {
            mainTasks.add(call.getArgument(1));
            return mock(BukkitTask.class);
        });
        doReturn(server).when(plugin).getServer();
        doReturn(pluginData.toFile()).when(plugin).getDataFolder();
        doReturn(Logger.getAnonymousLogger()).when(plugin).getLogger();
        doReturn(true).when(plugin).isEnabled();
        doNothing().when(plugin).saveDefaultConfig();
        doAnswer(call -> getClass().getResourceAsStream("/config.yml")).when(plugin).getResource("config.yml");
        try (var descriptor = getClass().getResourceAsStream("/plugin.yml")) {
            doReturn(new PluginDescriptionFile(descriptor)).when(plugin).getDescription();
        }
        var command = PluginCommandYamlParser.parse(plugin).get(0);
        CommandSender console = mock(CommandSender.class);
        when(console.hasPermission("mcqqauth.admin")).thenReturn(true);

        String trustStore = System.setProperty("javax.net.ssl.trustStore", keyStore.toString());
        String trustPassword = System.setProperty("javax.net.ssl.trustStorePassword", PASSWORD);
        String trustType = System.setProperty("javax.net.ssl.trustStoreType", "PKCS12");
        try {
            plugin.onEnable();
            Session original = take(api.sessions);
            take(api.requests).reply(200, snapshot(VERSION, "\"a\""));
            take(mainTasks).run();

            assertTrue(command.execute(console, "mcqqauth", new String[]{"mode", "enforce"}));
            assertTrue(command.execute(console, "mcqqauth", new String[0]));
            verify(console).sendMessage("授权版本：" + VERSION);
            verify(console).sendMessage("有效授权游戏ID：1");
            assertTrue(command.execute(console, "mcqqauth", new String[]{"mode", "observe"}));
            assertPong(original);
            assertNull(api.sessions.poll(200, MILLISECONDS));
            assertNull(api.requests.poll(200, MILLISECONDS));

            notice(original, VERSION + 1);
            take(api.requests).reply(200, snapshot(VERSION + 1, "\"a\",\"bb\""));
            take(mainTasks).run();
            assertTrue(command.execute(console, "mcqqauth", new String[0]));
            verify(console).sendMessage("授权版本：" + (VERSION + 1));
            verify(console).sendMessage("有效授权游戏ID：2");
        } finally {
            try {
                plugin.onDisable();
            } finally {
                restoreProperty("javax.net.ssl.trustStore", trustStore);
                restoreProperty("javax.net.ssl.trustStorePassword", trustPassword);
                restoreProperty("javax.net.ssl.trustStoreType", trustType);
            }
        }
    }

    @Test
    void coalescesNoticesAndDiscardsAResponseBehindAnInFlightUpdate() throws Exception {
        start(TOKEN, 3_000);
        Session session = take(api.sessions);
        take(api.requests).reply(200, snapshot(VERSION, "\"steve\""));
        take(updates);
        notice(session, VERSION);
        notice(session, VERSION - 1);
        assertNull(api.requests.poll(200, MILLISECONDS));

        notice(session, VERSION + 1);
        Exchange first = take(api.requests);
        assertFalse(service.view().ready());
        notice(session, VERSION + 1);
        notice(session, VERSION + 3);
        notice(session, VERSION + 2);
        assertNull(api.requests.poll(200, MILLISECONDS));
        first.reply(200, snapshot(VERSION + 1, "\"steve\""));
        take(api.requests).reply(200, snapshot(VERSION + 3, "\"alex\""));
        AuthorizationService.View updated = take(updates);
        assertEquals(VERSION + 3, updated.snapshot().version());
        assertFalse(updated.snapshot().allows("Steve"));
        assertTrue(updated.snapshot().allows("Alex"));
        assertNull(updates.poll(200, MILLISECONDS));
        assertTrue(api.requests.isEmpty());
    }

    @Test
    void disconnectInvalidatesLoginAndReconnectFetchesFullSnapshotWithoutNotice() throws Exception {
        start(TOKEN, 3_000);
        Session session = take(api.sessions);
        take(api.requests).reply(200, snapshot(VERSION, "\"steve\""));
        take(updates);
        session.disconnect();
        assertTrue(take(logs).contains("WSS"));
        assertFalse(service.view().ready());
        assertTrue(service.view().snapshot().allows("Steve"));
        assertTrue(updates.isEmpty());

        take(api.sessions);
        take(api.requests).reply(200, snapshot(VERSION - 1, "\"steve\""));
        assertTrue(take(logs).contains("版本落后"));
        assertFalse(service.view().ready());
        take(api.sessions);
        take(api.requests).reply(200, snapshot(VERSION + 1, ""));
        assertEquals(Set.of(), take(updates).snapshot().activeNames());
    }

    @Test
    void failedSnapshotKeepsLoginClosedAndRetriesWithBackoff() throws Exception {
        start(TOKEN, 700);
        take(api.sessions);
        take(api.requests).reply(401, TOKEN);
        String rejected = take(logs);
        assertTrue(rejected.contains("HTTP 401"));
        assertTrue(rejected.contains("100 毫秒"));
        assertFalse(rejected.contains(TOKEN));
        assertFalse(service.view().ready());

        take(api.sessions);
        take(api.requests);
        String timedOut = take(logs);
        assertTrue(timedOut.contains("Timeout"));
        assertTrue(timedOut.contains("200 毫秒"));
        assertFalse(service.view().ready());

        take(api.sessions);
        take(api.requests).reply(200, snapshot(VERSION, ""));
        assertTrue(take(updates).ready());
    }

    @Test
    void malformedPayloadsAndSameVersionConflictNeverReplaceConfirmedSnapshot() throws Exception {
        start(TOKEN, 3_000);
        take(api.sessions);
        take(api.requests).reply(200, "{\"version\":1,\"active_names\":[null]}");
        assertTrue(take(logs).contains("快照格式无效"));
        assertNull(service.view().snapshot());

        Session session = take(api.sessions);
        take(api.requests).reply(200, snapshot(VERSION, "\"steve\""));
        take(updates);
        send(session, "{\"type\":\"auth_updated\",\"version\":\"2\"}");
        assertTrue(take(logs).contains("通知格式无效"));
        take(api.sessions);
        take(api.requests).reply(200, snapshot(VERSION, ""));
        assertTrue(take(logs).contains("同一版本"));
        assertFalse(service.view().ready());
        assertTrue(service.view().snapshot().allows("Steve"));
        assertTrue(updates.isEmpty());
    }

    @Test
    void wrongWebsocketTokenIsRejectedBeforeAnySnapshot() throws Exception {
        start("wrong.test-token", 3_000);
        assertTrue(take(logs).contains("WSS"));
        assertFalse(service.view().ready());
        assertTrue(api.sessions.isEmpty());
        assertTrue(api.requests.isEmpty());
    }

    @Test
    void rejectsUntrustedCertificateAndHttpsHostnameMismatch() throws Exception {
        start(TOKEN, 3_000, new SslContextFactory.Client(), "localhost");
        assertTrue(take(logs).contains("WSS"));
        assertTrue(api.sessions.isEmpty());
        service.close();
        assertTrue(service.awaitStopped(Duration.ofSeconds(5)));

        start(TOKEN, 3_000, trustedLocalCertificate(), "127.0.0.1");
        take(api.sessions);
        assertTrue(take(logs).contains("HTTPS"));
        assertFalse(service.view().ready());
        assertTrue(api.requests.isEmpty());
    }

    @Test
    void closeCancelsInFlightSnapshotAndStopsOwnedThreads() throws Exception {
        start(TOKEN, 15_000);
        take(api.sessions);
        take(api.requests);
        service.close();
        assertFalse(service.view().ready());
        assertTrue(service.awaitStopped(Duration.ofSeconds(5)));
        assertEquals(1001, take(api.closes));
        await(() -> Thread.getAllStackTraces().keySet().stream()
                .noneMatch(thread -> thread.getName().startsWith("MCQQAuth-")));
        assertNull(api.sessions.poll(250, MILLISECONDS));
        assertTrue(updates.isEmpty());
    }

    @Test
    void concurrentReadsSeeOneConsistentVersionAndRoster() throws Exception {
        start(TOKEN, 3_000);
        Session session = take(api.sessions);
        take(api.requests).reply(200, snapshot(VERSION, "\"steve\""));
        take(updates);
        AtomicBoolean reading = new AtomicBoolean(true);
        AtomicReference<Throwable> failure = new AtomicReference<>();
        var readers = new ArrayList<Thread>();
        for (int i = 0; i < 4; i++) {
            Thread reader = new Thread(() -> {
                while (reading.get()) {
                    try {
                        AuthProtocol.Snapshot current = service.view().snapshot();
                        if (current.version() == VERSION) {
                            assertEquals(Set.of("steve"), current.activeNames());
                        } else {
                            assertEquals(VERSION + 1, current.version());
                            assertEquals(Set.of("alex"), current.activeNames());
                        }
                    } catch (Throwable e) {
                        failure.compareAndSet(null, e);
                        return;
                    }
                }
            });
            reader.setDaemon(true);
            reader.start();
            readers.add(reader);
        }
        try {
            notice(session, VERSION + 1);
            take(api.requests).reply(200, snapshot(VERSION + 1, "\"alex\""));
            take(updates);
        } finally {
            reading.set(false);
            for (Thread reader : readers) {
                reader.join();
            }
        }
        assertNull(failure.get());
    }

    private void start(String token, int requestTimeout) {
        start(token, requestTimeout, trustedLocalCertificate(), "localhost");
    }

    private void start(String token, int requestTimeout, SslContextFactory.Client tls, String snapshotHost) {
        Logger logger = Logger.getAnonymousLogger();
        logger.setUseParentHandlers(false);
        logger.addHandler(new java.util.logging.Handler() {
            @Override public void publish(LogRecord record) { logs.add(record.getMessage()); }
            @Override public void flush() {}
            @Override public void close() {}
        });
        int port = api.port();
        service = new AuthorizationService(new PluginSettings(
                URI.create("https://" + snapshotHost + ":" + port + "/internal/mc/snapshot"),
                URI.create("wss://localhost:" + port + "/internal/mc/ws"),
                token, 2_000, requestTimeout, 100, 400), tls, logger, () -> updates.add(service.view()));
        service.start();
    }

    private static SslContextFactory.Client trustedLocalCertificate() {
        SslContextFactory.Client tls = new SslContextFactory.Client();
        tls.setTrustStorePath(keyStore.toString());
        tls.setTrustStorePassword(PASSWORD);
        return tls;
    }

    private static String snapshot(long version, String names) {
        return "{\"version\":" + version + ",\"active_names\":[" + names + "]}";
    }

    private static void notice(Session session, long version) throws Exception {
        send(session, "{\"type\":\"auth_updated\",\"version\":" + version + "}");
    }

    private static void send(Session session, String text) throws Exception {
        CompletableFuture<Void> sent = new CompletableFuture<>();
        session.sendText(text, Callback.from(() -> sent.complete(null), sent::completeExceptionally));
        sent.get(5, SECONDS);
    }

    private void assertPong(Session session) throws Exception {
        ByteBuffer payload = ByteBuffer.wrap(new byte[]{1, 2, 3, 4});
        CompletableFuture<Void> sent = new CompletableFuture<>();
        session.sendPing(payload.asReadOnlyBuffer(),
                Callback.from(() -> sent.complete(null), sent::completeExceptionally));
        sent.get(5, SECONDS);
        assertEquals(payload, take(api.pongs));
    }

    private static void restoreProperty(String key, String value) {
        if (value == null) {
            System.clearProperty(key);
        } else {
            System.setProperty(key, value);
        }
    }

    private <T> T take(BlockingQueue<T> queue) throws InterruptedException {
        T item = queue.poll(5, SECONDS);
        assertNotNull(item, () -> "expected an event from the local server or client, client logs: " + logs);
        return item;
    }

    private static void await(BooleanSupplier condition) throws InterruptedException {
        long deadline = System.nanoTime() + SECONDS.toNanos(5);
        while (!condition.getAsBoolean() && System.nanoTime() < deadline) {
            Thread.sleep(10);
        }
        assertTrue(condition.getAsBoolean());
    }

    private record Exchange(Response response, org.eclipse.jetty.util.Callback callback) {
        void reply(int status, String body) {
            response.setStatus(status);
            response.getHeaders().put(HttpHeader.CONTENT_TYPE, "application/json");
            response.write(true, ByteBuffer.wrap(body.getBytes(StandardCharsets.UTF_8)), callback);
        }
    }

    private static final class LocalApi implements AutoCloseable {
        private final BlockingQueue<Session> sessions = new LinkedBlockingQueue<>();
        private final BlockingQueue<Exchange> requests = new LinkedBlockingQueue<>();
        private final BlockingQueue<Integer> closes = new LinkedBlockingQueue<>();
        private final BlockingQueue<ByteBuffer> pongs = new LinkedBlockingQueue<>();
        private final Server server = new Server();
        private final ServerConnector connector;

        LocalApi() throws Exception {
            var tls = new SslContextFactory.Server();
            tls.setKeyStorePath(keyStore.toString());
            tls.setKeyStorePassword(PASSWORD);
            var https = new HttpConfiguration();
            https.addCustomizer(new SecureRequestCustomizer());
            connector = new ServerConnector(server, new SslConnectionFactory(tls, "http/1.1"),
                    new HttpConnectionFactory(https));
            connector.setHost("127.0.0.1");
            server.addConnector(connector);

            ServerWebSocketContainer sockets = ServerWebSocketContainer.ensure(server);
            sockets.addMapping("/internal/mc/ws", (request, response, callback) -> {
                if (!authenticated(request)) {
                    response.setStatus(403);
                    callback.succeeded();
                    return null;
                }
                return new TestSocket(sessions, closes, pongs);
            });
            server.setHandler(new Handler.Abstract() {
                @Override
                public boolean handle(Request request, Response response, org.eclipse.jetty.util.Callback callback) {
                    if (sockets.handle(request, response, callback)) {
                        return true;
                    }
                    if (!request.getHttpURI().getPath().equals("/internal/mc/snapshot")) {
                        return false;
                    }
                    Exchange exchange = new Exchange(response, callback);
                    if (authenticated(request) && request.getMethod().equals("GET")) {
                        requests.add(exchange);
                    } else {
                        exchange.reply(401, "{}");
                    }
                    return true;
                }
            });
            server.start();
        }

        private static boolean authenticated(Request request) {
            return ("Bearer " + TOKEN).equals(request.getHeaders().get(HttpHeader.AUTHORIZATION));
        }

        int port() {
            return connector.getLocalPort();
        }

        @Override
        public void close() throws Exception {
            server.stop();
        }
    }

    public record TestSocket(BlockingQueue<Session> sessions, BlockingQueue<Integer> closes,
                             BlockingQueue<ByteBuffer> pongs)
            implements Session.Listener.AutoDemanding {
        @Override
        public void onWebSocketOpen(Session session) {
            session.sendText("{\"type\":\"connected\",\"message\":\"test\"}",
                    Callback.from(() -> sessions.add(session), failure -> session.disconnect()));
        }

        @Override
        public void onWebSocketClose(int statusCode, String reason) {
            closes.add(statusCode);
        }

        @Override
        public void onWebSocketPong(ByteBuffer payload) {
            pongs.add(payload);
        }
    }
}
