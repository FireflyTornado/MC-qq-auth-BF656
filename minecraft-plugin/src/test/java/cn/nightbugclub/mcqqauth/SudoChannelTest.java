package cn.nightbugclub.mcqqauth;

import com.google.gson.JsonParser;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.nio.ByteBuffer;
import java.time.Duration;
import java.util.ArrayDeque;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.logging.Logger;

import static java.util.concurrent.TimeUnit.MILLISECONDS;
import static java.util.concurrent.TimeUnit.SECONDS;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class SudoChannelTest {
    @Test
    void failedHandshakesBackOffAndOpeningResetsTheDelay() {
        try (var fixture = new ChannelFixture()) {
            when(fixture.builder.buildAsync(any(URI.class), any(WebSocket.Listener.class)))
                    .thenReturn(CompletableFuture.failedFuture(new IllegalStateException("handshake failed")));
            fixture.channel.start();
            fixture.runNext(0, MILLISECONDS);
            for (int delay : new int[]{1000, 2000, 4000, 8000, 16000, 30000, 30000}) {
                fixture.runNext(delay, MILLISECONDS);
            }

            Peer peer = fixture.openNext(30000);
            ScheduledTask firstPing = fixture.takeNext(20, SECONDS);
            peer.disconnect();
            verify(firstPing.future()).cancel(false);
            fixture.takeNext(1000, MILLISECONDS);
            assertTrue(fixture.scheduled.isEmpty());
        }
    }

    @Test
    void onlyMatchingPongSchedulesNextPingAndMissingPongReconnects() {
        try (var fixture = new ChannelFixture()) {
            Peer peer = fixture.connect();
            peer.listener.onPong(peer.socket, ByteBuffer.allocate(0));
            fixture.runNext(20, SECONDS);
            ScheduledTask firstTimeout = fixture.takeNext(15, SECONDS);
            long firstPing = peer.pings.remove();

            peer.pong(firstPing + 1);
            verifyNoInteractions(firstTimeout.future());
            assertTrue(fixture.scheduled.isEmpty());
            peer.pong(firstPing);
            verify(firstTimeout.future()).cancel(false);
            fixture.runNext(20, SECONDS);
            ScheduledTask secondTimeout = fixture.takeNext(15, SECONDS);
            assertEquals(firstPing + 1, peer.pings.remove());

            peer.pong(firstPing);
            firstTimeout.action().run();
            verify(peer.socket, never()).abort();
            secondTimeout.action().run();
            verify(peer.socket).abort();
            fixture.takeNext(1000, MILLISECONDS);
            assertTrue(fixture.scheduled.isEmpty());
        }
    }

    @Test
    void reconnectReleasesQueuedRequestWithoutReleasingItsReplacement() {
        try (var fixture = new ChannelFixture()) {
            var original = fixture.connect();
            original.send(1);
            original.disconnect();

            var reconnected = fixture.openNext(1000);
            reconnected.send(1);
            assertEquals("rejected", reconnected.status());
            reconnected.send(2);
            assertEquals(2, fixture.tasks.size());
            fixture.tasks.remove().run();
            verifyNoInteractions(fixture.commands);
            reconnected.send(3);
            assertEquals("busy", reconnected.status());
            fixture.tasks.remove().run();
            assertEquals("ok", reconnected.status());
            verify(fixture.commands).execute(eq("test2"), anyLong());
            verifyNoMoreInteractions(fixture.commands);
            assertTrue(original.replies.isEmpty());
        }
    }

    @Test
    void reconnectKeepsSlotWhileOldCommandIsExecuting() {
        try (var fixture = new ChannelFixture()) {
            var original = fixture.connect();
            var reconnected = new ArrayDeque<Peer>();
            when(fixture.commands.execute(eq("test1"), anyLong())).thenAnswer(call -> {
                original.disconnect();
                var next = fixture.openNext(1000);
                next.send(2);
                assertEquals("busy", next.status());
                assertTrue(fixture.tasks.isEmpty());
                reconnected.add(next);
                return CompletableFuture.completedFuture(new SudoCommands.Result("ok"));
            });
            original.send(1);
            fixture.tasks.remove().run();

            var next = reconnected.remove();
            next.send(3);
            fixture.tasks.remove().run();
            assertEquals("ok", next.status());
            verify(fixture.commands).execute(eq("test1"), anyLong());
            verify(fixture.commands).execute(eq("test3"), anyLong());
            verifyNoMoreInteractions(fixture.commands);
            assertTrue(original.replies.isEmpty());
        }
    }

    @Test
    void repliesOnlyAfterOutputCollectionCompletes() {
        try (var fixture = new ChannelFixture()) {
            var peer = fixture.connect();
            var result = new CompletableFuture<SudoCommands.Result>();
            when(fixture.commands.execute(eq("test1"), anyLong())).thenReturn(result);

            peer.send(1);
            fixture.tasks.remove().run();
            assertTrue(peer.replies.isEmpty());
            peer.send(2);
            assertEquals("busy", peer.status());
            result.complete(new SudoCommands.Result("ok", "some output\n", false, true));
            var reply = JsonParser.parseString(peer.replies.remove()).getAsJsonObject();
            assertEquals("ok", reply.get("status").getAsString());
            assertTrue(reply.get("capture_incomplete").getAsBoolean());

            peer.send(3);
            fixture.tasks.remove().run();
            assertEquals("ok", peer.status());
        }
    }

    @Test
    void disconnectCancelsOutputCollectionAndReleasesTheSlot() {
        try (var fixture = new ChannelFixture()) {
            var peer = fixture.connect();
            var collection = new CompletableFuture<SudoCommands.Result>();
            when(fixture.commands.execute(eq("test1"), anyLong())).thenReturn(collection);
            peer.send(1);
            fixture.tasks.remove().run();
            peer.disconnect();
            assertTrue(collection.isCancelled());
            var next = fixture.openNext(1000);
            next.send(2);
            fixture.tasks.remove().run();
            assertEquals("ok", next.status());
            assertTrue(peer.replies.isEmpty());
        }
    }

    private static final class ChannelFixture implements AutoCloseable {
        final ArrayDeque<Runnable> tasks = new ArrayDeque<>();
        final ArrayDeque<ScheduledTask> scheduled = new ArrayDeque<>();
        final SudoCommands commands = mock(SudoCommands.class);
        final WebSocket.Builder builder = mock(WebSocket.Builder.class, RETURNS_SELF);
        final SudoChannel channel;

        ChannelFixture() {
            var http = mock(HttpClient.class);
            var timer = mock(ScheduledExecutorService.class);
            when(timer.schedule(any(Runnable.class), anyLong(), any(TimeUnit.class))).thenAnswer(call -> {
                var future = mock(ScheduledFuture.class);
                var task = new ScheduledTask(call.getArgument(0), call.getArgument(1), call.getArgument(2), future);
                when(future.cancel(false)).thenAnswer(cancel -> scheduled.remove(task));
                scheduled.add(task);
                return future;
            });
            when(http.newWebSocketBuilder()).thenReturn(builder);
            when(commands.execute(anyString(), anyLong()))
                    .thenReturn(CompletableFuture.completedFuture(new SudoCommands.Result("ok")));
            var settings = new SudoChannel.Settings(URI.create("wss://localhost/sudo"), "test-token");
            channel = new SudoChannel(settings, commands, tasks::add, mock(Logger.class), 1000, 30000, http, timer);
        }

        Peer connect() {
            channel.start();
            return openNext(0);
        }

        Peer openNext(int delayMs) {
            var opened = new ArrayDeque<Peer>();
            doAnswer(call -> {
                var peer = new Peer(call.getArgument(1));
                peer.listener.onOpen(peer.socket);
                opened.add(peer);
                return CompletableFuture.completedFuture(peer.socket);
            }).when(builder).buildAsync(any(URI.class), any(WebSocket.Listener.class));
            runNext(delayMs, MILLISECONDS);
            return opened.remove();
        }

        ScheduledTask takeNext(long delay, TimeUnit unit) {
            ScheduledTask next = scheduled.remove();
            assertEquals(delay, next.delay());
            assertEquals(unit, next.unit());
            return next;
        }

        void runNext(long delay, TimeUnit unit) {
            takeNext(delay, unit).action().run();
        }

        @Override
        public void close() {
            channel.close();
        }
    }

    private record ScheduledTask(Runnable action, long delay, TimeUnit unit, ScheduledFuture<?> future) {}

    private static final class Peer {
        final WebSocket.Listener listener;
        final WebSocket socket = mock(WebSocket.class);
        final ArrayDeque<String> replies = new ArrayDeque<>();
        final ArrayDeque<Long> pings = new ArrayDeque<>();

        Peer(WebSocket.Listener listener) {
            this.listener = listener;
            when(socket.sendPing(any(ByteBuffer.class))).thenAnswer(call -> {
                pings.add(call.<ByteBuffer>getArgument(0).getLong());
                return CompletableFuture.completedFuture(socket);
            });
            when(socket.sendText(anyString(), eq(true))).thenAnswer(call -> {
                replies.add(call.getArgument(0));
                return CompletableFuture.completedFuture(socket);
            });
        }

        void send(int id) {
            long deadline = System.currentTimeMillis() + Duration.ofSeconds(25).toMillis();
            String message = """
                    {"id":"%032x","command":"test%d","deadline_ms":%d}
                    """.formatted(id, id, deadline);
            listener.onText(socket, message, true);
        }

        String status() {
            return JsonParser.parseString(replies.remove()).getAsJsonObject().get("status").getAsString();
        }

        void disconnect() {
            listener.onClose(socket, 1000, "");
        }

        void pong(long ping) {
            listener.onPong(socket, ByteBuffer.allocate(Long.BYTES).putLong(ping).flip());
        }
    }

    @Test
    void requestRejectedWhileBusyCannotRunOnReplay() {
        var history = new SudoChannel.RequestHistory();
        var request = new SudoChannel.Request("original", "list", 20_000);
        var refused = new SudoChannel.Request("refused", "say hello", 20_000);
        var next = new SudoChannel.Request("new", "say hello", 20_000);

        assertNull(history.admit(request, 1_000, false));
        assertEquals("busy", history.admit(refused, 2_000, true));
        assertEquals("rejected", history.admit(refused, 3_000, false));
        assertNull(history.admit(next, 3_000, false));
    }

    @Test
    void fullHistoryKeepsRecordsAndBlocksUntrackedReplaysUntilTheirDeadline() {
        var history = new SudoChannel.RequestHistory();
        var original = new SudoChannel.Request("0", "list", 10_000);
        assertNull(history.admit(original, 1_000, false));
        for (int id = 1; id < 1024; id++) {
            var request = new SudoChannel.Request(Integer.toString(id), "list", 10_000);
            assertEquals("busy", history.admit(request, 1_000, true));
        }

        var refused = new SudoChannel.Request("overflow", "list", 20_000);
        assertEquals("busy", history.admit(refused, 2_000, false));
        assertEquals("rejected", history.admit(original, 3_000, false));
        assertEquals("busy", history.admit(refused, 11_000, false));

        var next = new SudoChannel.Request("fresh", "list", 30_000);
        assertNull(history.admit(next, 20_000, false));
    }
}
