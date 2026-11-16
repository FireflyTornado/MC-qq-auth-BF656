package cn.nightbugclub.mcqqauth;

import java.util.concurrent.CompletableFuture;
import java.util.function.LongSupplier;

import static java.nio.charset.StandardCharsets.UTF_8;
import static java.util.concurrent.TimeUnit.MILLISECONDS;
import static java.util.concurrent.TimeUnit.NANOSECONDS;

final class BoundedCommandOutput {
    static final int MAX_BYTES = 64 * 1024;
    static final long MAX_SENDER_WAIT_MS = 3_000;
    private static final long QUIET_NANOS = MILLISECONDS.toNanos(500);
    private static final long CHECK_NANOS = MILLISECONDS.toNanos(50);
    static final String TRUNCATION_NOTICE = "\n[输出超过 64 KiB，已截断。]\n";
    private static final int NOTICE_BYTES = TRUNCATION_NOTICE.getBytes(UTF_8).length;

    record Snapshot(String text, boolean truncated) {}

    private final StringBuilder text = new StringBuilder();
    private final LongSupplier clock;
    private int bytes;
    private boolean truncated;
    private boolean closed;
    private Long lastSenderText;
    private Long senderDeadline;

    BoundedCommandOutput() {
        this(System::nanoTime);
    }

    BoundedCommandOutput(LongSupplier clock) {
        this.clock = clock;
    }

    synchronized boolean append(String part) {
        return append(part, false);
    }

    synchronized boolean appendSender(String part) {
        return append(part, true);
    }

    private boolean append(String part, boolean sender) {
        if (!acceptingOutput()) {
            return true;
        }
        boolean visibleSenderText = false;
        for (int offset = 0; !truncated && offset < part.length();) {
            int codePoint = part.codePointAt(offset);
            offset += Character.charCount(codePoint);
            if (codePoint >= Character.MIN_SURROGATE && codePoint <= Character.MAX_SURROGATE) {
                codePoint = '?';
            }
            int size = utf8Bytes(codePoint);
            if (bytes + size <= MAX_BYTES) {
                text.appendCodePoint(codePoint);
                bytes += size;
                visibleSenderText |= sender && !Character.isISOControl(codePoint)
                        && !Character.isWhitespace(codePoint) && !Character.isSpaceChar(codePoint);
            } else {
                while (bytes + NOTICE_BYTES > MAX_BYTES) {
                    int last = text.codePointBefore(text.length());
                    text.setLength(text.length() - Character.charCount(last));
                    bytes -= utf8Bytes(last);
                }
                text.append(TRUNCATION_NOTICE);
                truncated = true;
            }
        }
        if (visibleSenderText) {
            long now = clock.getAsLong();
            if (senderDeadline == null || remainingSenderWait(now) > 0) {
                lastSenderText = now;
            }
        }
        return truncated;
    }

    synchronized Snapshot snapshot() {
        return new Snapshot(text.toString(), truncated);
    }

    synchronized void appendLine(String line) {
        if (!text.isEmpty() && text.charAt(text.length() - 1) != '\n') {
            append("\n");
        }
        append(line);
        if (!line.endsWith("\n")) {
            append("\n");
        }
    }

    synchronized boolean acceptingOutput() {
        return !closed && !truncated;
    }

    synchronized void startSenderWait(long millis) {
        long now = clock.getAsLong();
        senderDeadline = now + MILLISECONDS.toNanos(millis);
        if (lastSenderText != null) {
            lastSenderText = now;
        }
    }

    synchronized long senderWaitNanos() {
        if (!acceptingOutput()) {
            return 0;
        }
        return remainingSenderWait(clock.getAsLong());
    }

    private long remainingSenderWait(long now) {
        long remaining = senderDeadline - now;
        if (lastSenderText != null) {
            remaining = Math.min(remaining, QUIET_NANOS - (now - lastSenderText));
        }
        return Math.max(0, remaining);
    }

    CompletableFuture<Void> awaitSender(long millis) {
        startSenderWait(millis);
        var done = new CompletableFuture<Void>();
        checkSender(done);
        return done;
    }

    private void checkSender(CompletableFuture<Void> done) {
        long delay = senderWaitNanos();
        if (delay == 0) {
            done.complete(null);
        } else if (!done.isDone()) {
            CompletableFuture.delayedExecutor(Math.min(delay, CHECK_NANOS), NANOSECONDS)
                    .execute(() -> checkSender(done));
        }
    }

    synchronized Snapshot finish() {
        close();
        return snapshot();
    }

    synchronized void close() {
        closed = true;
    }

    private static int utf8Bytes(int codePoint) {
        if (codePoint <= 0x7f) {
            return 1;
        }
        if (codePoint <= 0x7ff) {
            return 2;
        }
        return codePoint <= 0xffff ? 3 : 4;
    }
}
