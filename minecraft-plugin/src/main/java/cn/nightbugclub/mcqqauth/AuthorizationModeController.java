package cn.nightbugclub.mcqqauth;

import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;

final class AuthorizationModeController {
    static final class Policy {
        final PluginSettings.Mode mode;
        private final AtomicBoolean syncQueued = new AtomicBoolean();

        Policy(PluginSettings.Mode mode) {
            this.mode = mode;
        }
    }

    private volatile Policy policy;

    AuthorizationModeController(PluginSettings.Mode initialMode) {
        switchTo(initialMode);
    }

    PluginSettings.Mode mode() {
        Policy current = policy;
        return current == null ? null : current.mode;
    }

    // Only the Bukkit main thread changes the policy. A new identity invalidates queued work.
    void switchTo(PluginSettings.Mode mode) {
        policy = new Policy(mode);
    }

    void close() {
        policy = null;
    }

    boolean isCurrent(Policy expected) {
        return policy == expected;
    }

    void scheduleSync(Consumer<Runnable> runOnMainThread, Consumer<Policy> action) {
        Policy expected = policy;
        if (expected != null && expected.syncQueued.compareAndSet(false, true)) {
            runOnMainThread.accept(() -> {
                expected.syncQueued.set(false);
                if (isCurrent(expected)) {
                    action.accept(expected);
                }
            });
        }
    }
}
