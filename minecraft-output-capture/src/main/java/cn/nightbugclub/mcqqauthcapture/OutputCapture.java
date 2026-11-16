package cn.nightbugclub.mcqqauthcapture;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.core.LogEvent;
import org.apache.logging.log4j.core.LoggerContext;
import org.apache.logging.log4j.core.appender.AbstractAppender;
import org.apache.logging.log4j.core.config.Property;

import java.io.PrintWriter;
import java.io.StringWriter;
import java.util.ArrayDeque;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;
import java.util.function.Function;
import java.util.function.Supplier;

public final class OutputCapture {
    private static final ThreadLocal<Frame> ACTIVE = new ThreadLocal<>();
    private static final CommandLog LOG = new CommandLog();

    private OutputCapture() {}

    // The Bukkit plugin loads this bridge through the server class loader. The contract uses only JDK types.
    public static Supplier<CompletableFuture<Boolean>> begin(Consumer<String> output) {
        attachLogCollector();
        var session = new Session(output);
        Frame previous = ACTIVE.get();
        ACTIVE.set(new Frame(session));
        return () -> {
            restore(previous);
            session.finish(true);
            return session.completed;
        };
    }

    public static <T> Consumer<T> track(Consumer<T> callback) {
        Frame frame = ACTIVE.get();
        if (frame == null || !frame.session.register()) {
            return callback;
        }
        return tracked(frame.session, (T value) -> {
            callback.accept(value);
            return null;
        })::apply;
    }

    public static <T> CompletableFuture<T> trackReload(CompletableFuture<T> reload,
                                                        Function<Throwable, ? extends T> recovery) {
        Frame frame = ACTIVE.get();
        if (frame == null || !frame.session.register()) {
            return reload.exceptionally(recovery);
        }
        var callback = tracked(frame.session, (Throwable failure) -> failure == null ? null : recovery.apply(failure));
        return reload.handle((value, failure) -> {
            T recovered = callback.apply(failure);
            return failure == null ? value : recovered;
        });
    }

    private static <T, R> Function<T, R> tracked(Session session, Function<T, R> callback) {
        var finished = new AtomicBoolean();
        return value -> {
            if (!finished.compareAndSet(false, true) || session.completed.isDone()) {
                return callback.apply(value);
            }
            Frame previous = ACTIVE.get();
            ACTIVE.set(new Frame(session));
            boolean success = false;
            try {
                R result = callback.apply(value);
                success = true;
                return result;
            } finally {
                restore(previous);
                session.finish(success);
            }
        };
    }

    public static void runQueuedTask(Runnable task) {
        Frame previous = ACTIVE.get();
        if (previous == null) {
            task.run();
            return;
        }
        // Commands such as reload can pump the game queue while dispatch is still on the stack.
        ACTIVE.remove();
        try {
            task.run();
        } finally {
            ACTIVE.set(previous);
        }
    }

    public static void beginBroadcast(Object message, Object chatType) {
        Frame frame = ACTIVE.get();
        if (frame == null || frame.session.completed.isDone()) {
            return;
        }
        String body;
        try {
            body = VanillaOutput.broadcast(message, chatType);
        } catch (ReflectiveOperationException | RuntimeException e) {
            frame.broadcasts.push(new Broadcast(null));
            frame.session.incomplete();
            return;
        }
        beginBroadcast(body);
    }

    static void beginBroadcast(String body) {
        Frame frame = ACTIVE.get();
        frame.broadcasts.push(new Broadcast(body));
        frame.session.append(body);
    }

    public static void endBroadcast() {
        Frame frame = ACTIVE.get();
        if (frame != null) {
            frame.broadcasts.poll();
        }
    }

    public static void packet(Object connection, Object packet) {
        Frame frame = ACTIVE.get();
        if (frame == null || frame.session.completed.isDone()) {
            return;
        }
        try {
            String output = VanillaOutput.packet(connection, packet, frame.broadcastBody());
            if (output != null) {
                frame.session.append(output);
            }
        } catch (ReflectiveOperationException | RuntimeException e) {
            frame.session.incomplete();
        }
    }

    private static void restore(Frame previous) {
        if (previous == null) {
            ACTIVE.remove();
        } else {
            ACTIVE.set(previous);
        }
    }

    private static synchronized void attachLogCollector() {
        var context = (LoggerContext) LogManager.getContext(false);
        var root = context.getRootLogger();
        if (root.getAppenders().get(LOG.getName()) != LOG) {
            LOG.start();
            root.addAppender(LOG);
        }
    }

    private static final class Frame {
        final Session session;
        final ArrayDeque<Broadcast> broadcasts = new ArrayDeque<>();

        Frame(Session session) {
            this.session = session;
        }

        String broadcastBody() {
            Broadcast broadcast = broadcasts.peek();
            return broadcast == null ? null : broadcast.body();
        }
    }

    private record Broadcast(String body) {}

    private static final class Session {
        final CompletableFuture<Boolean> completed = new CompletableFuture<>();
        private final Consumer<String> output;
        private int pending = 1; // Dispatch itself owns the first slot.
        private boolean complete = true;

        Session(Consumer<String> output) {
            this.output = output;
        }

        synchronized boolean register() {
            if (completed.isDone()) {
                return false;
            }
            pending++;
            return true;
        }

        synchronized void append(String text) {
            if (!completed.isDone()) {
                output.accept(text);
            }
        }

        synchronized void incomplete() {
            complete = false;
        }

        synchronized void finish(boolean success) {
            complete &= success;
            if (--pending == 0) {
                completed.complete(complete);
            }
        }
    }

    private static final class CommandLog extends AbstractAppender {
        CommandLog() {
            super("MCQQAuthCommandOutput", null, null, true, Property.EMPTY_ARRAY);
        }

        @Override
        public void append(LogEvent event) {
            Frame frame = ACTIVE.get();
            if (frame == null || frame.session.completed.isDone()) {
                return;
            }
            String message = event.getMessage().getFormattedMessage();
            String broadcast = frame.broadcastBody();
            if (broadcast != null && event.getThrown() == null
                    && "net.minecraft.server.MinecraftServer".equals(event.getLoggerName())
                    && (message.equals(broadcast) || message.equals("[Not Secure] " + broadcast))) {
                return;
            }
            frame.session.append(message);
            if (event.getThrown() != null) {
                var stack = new StringWriter();
                event.getThrown().printStackTrace(new PrintWriter(stack));
                frame.session.append(stack.toString());
            }
        }
    }
}
