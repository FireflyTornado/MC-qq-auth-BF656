package cn.nightbugclub.mcqqauth;

import com.google.gson.JsonParser;
import org.junit.jupiter.api.Test;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.time.Duration;
import java.util.ArrayDeque;
import java.util.concurrent.CompletableFuture;
import java.util.logging.Logger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class SudoChannelTest {
    @Test
    void reconnectReleasesQueuedRequestWithoutReleasingItsReplacement() {
        try (var fixture = new ChannelFixture()) {
            var original = fixture.connect();
            original.send(1);
            original.disconnect();

            var reconnected = fixture.connect();
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
            verify(fixture.commands).execute("test2");
            verifyNoMoreInteractions(fixture.commands);
            assertTrue(original.replies.isEmpty());
        }
    }

    @Test
    void reconnectKeepsSlotWhileOldCommandIsExecuting() {
        try (var fixture = new ChannelFixture()) {
            var original = fixture.connect();
            var reconnected = new ArrayDeque<Peer>();
            when(fixture.commands.execute("test1")).thenAnswer(call -> {
                original.disconnect();
                var next = fixture.connect();
                next.send(2);
                assertEquals("busy", next.status());
                assertTrue(fixture.tasks.isEmpty());
                reconnected.add(next);
                return new SudoCommands.Result("ok");
            });
            original.send(1);
            fixture.tasks.remove().run();

            var next = reconnected.remove();
            next.send(3);
            fixture.tasks.remove().run();
            assertEquals("ok", next.status());
            verify(fixture.commands).execute("test1");
            verify(fixture.commands).execute("test3");
            verifyNoMoreInteractions(fixture.commands);
            assertTrue(original.replies.isEmpty());
        }
    }

    private static final class ChannelFixture implements AutoCloseable {
        final ArrayDeque<Runnable> tasks = new ArrayDeque<>();
        final SudoCommands commands = mock(SudoCommands.class);
        final WebSocket.Builder builder = mock(WebSocket.Builder.class, RETURNS_SELF);
        final SudoChannel channel;

        ChannelFixture() {
            var http = mock(HttpClient.class);
            when(http.newWebSocketBuilder()).thenReturn(builder);
            when(commands.execute(anyString())).thenReturn(new SudoCommands.Result("ok"));
            var settings = new SudoChannel.Settings(URI.create("wss://localhost/sudo"), "test-token");
            channel = new SudoChannel(settings, commands, tasks::add, mock(Logger.class), http);
        }

        Peer connect() {
            var opened = new ArrayDeque<Peer>();
            doAnswer(call -> {
                var peer = new Peer(call.getArgument(1));
                peer.listener.onOpen(peer.socket);
                opened.add(peer);
                return CompletableFuture.completedFuture(peer.socket);
            }).when(builder).buildAsync(any(URI.class), any(WebSocket.Listener.class));
            channel.maintain();
            return opened.remove();
        }

        @Override
        public void close() {
            channel.close();
        }
    }

    private static final class Peer {
        final WebSocket.Listener listener;
        final WebSocket socket = mock(WebSocket.class);
        final ArrayDeque<String> replies = new ArrayDeque<>();

        Peer(WebSocket.Listener listener) {
            this.listener = listener;
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
