package cn.nightbugclub.mcqqauthcapture;

import org.apache.logging.log4j.LogManager;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;

import static org.junit.jupiter.api.Assertions.*;

class OutputCaptureTest {
    @Test
    void deduplicatesTheConsoleCopyWithinEachBroadcastOnly() {
        var log = LogManager.getLogger("net.minecraft.server.MinecraftServer");
        var output = new ArrayList<String>();
        var seal = OutputCapture.begin(output::add);
        for (int broadcast = 0; broadcast < 2; broadcast++) {
            OutputCapture.beginBroadcast("same text");
            log.error("same text");
            OutputCapture.endBroadcast();
        }
        log.error("same text");
        assertTrue(seal.get().join());
        assertEquals(List.of("same text", "same text", "same text"), output);
    }

    @Test
    void attributesLogsFromDelayedCallbacksToTheirOriginalRequest() throws Exception {
        var log = LogManager.getLogger("test.command");
        var first = new ArrayList<String>();
        var sealFirst = OutputCapture.begin(first::add);
        var callback = OutputCapture.track((Consumer<String>) log::error);
        log.error("first synchronous line");
        var firstResult = sealFirst.get();
        assertFalse(firstResult.isDone());

        var second = new ArrayList<String>();
        var sealSecond = OutputCapture.begin(second::add);
        log.error("second request");
        CompletableFuture.runAsync(() -> log.error("unrelated thread")).get(2, TimeUnit.SECONDS);
        CompletableFuture.runAsync(() -> callback.accept("first delayed line")).get(2, TimeUnit.SECONDS);
        assertTrue(sealSecond.get().join());
        assertTrue(firstResult.join());
        assertEquals(List.of("first synchronous line", "first delayed line"), first);
        assertEquals(List.of("second request"), second);
    }

    @Test
    void excludesUnrelatedTasksPumpedOnTheCommandThread() {
        var log = LogManager.getLogger("test.command");
        var output = new ArrayList<String>();
        var seal = OutputCapture.begin(output::add);
        var callback = OutputCapture.track((Consumer<String>) log::error);
        OutputCapture.runQueuedTask(() -> {
            log.error("unrelated task");
            callback.accept("tracked callback");
        });
        log.error("dispatch resumes");
        assertTrue(seal.get().join());
        assertEquals(List.of("tracked callback", "dispatch resumes"), output);
    }

    @Test
    void keepsNativeAsyncCallbacksWorkingAfterCaptureHasTimedOut() {
        var output = new ArrayList<String>();
        var seal = OutputCapture.begin(output::add);
        var callbackCalled = new CompletableFuture<String>();
        var callback = OutputCapture.track((Consumer<String>) value -> {
            LogManager.getLogger("test.command").error("late message");
            callbackCalled.complete(value);
        });
        seal.get().complete(false);
        callback.accept("original callback");
        assertEquals("original callback", callbackCalled.join());
        assertTrue(output.isEmpty());
    }

    @Test
    void waitsForReloadAndCapturesItsFailureHandler() {
        var output = new ArrayList<String>();
        var seal = OutputCapture.begin(output::add);
        var reload = new CompletableFuture<Void>();
        var recovered = OutputCapture.trackReload(reload, failure -> {
            LogManager.getLogger("test.reload").error(failure.getMessage());
            return null;
        });
        var result = seal.get();
        assertFalse(result.isDone());
        reload.completeExceptionally(new IllegalStateException("reload failed"));
        assertNull(recovered.join());
        assertTrue(result.join());
        assertEquals(List.of("reload failed"), output);
    }
}
