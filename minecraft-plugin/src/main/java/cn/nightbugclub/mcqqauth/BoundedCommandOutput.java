package cn.nightbugclub.mcqqauth;

import static java.nio.charset.StandardCharsets.UTF_8;

final class BoundedCommandOutput {
    static final int MAX_BYTES = 64 * 1024;
    static final String TRUNCATION_NOTICE = "\n[输出超过 64 KiB，已截断。]\n";
    private static final int NOTICE_BYTES = TRUNCATION_NOTICE.getBytes(UTF_8).length;

    record Snapshot(String text, boolean truncated) {}

    private final StringBuilder text = new StringBuilder();
    private int bytes;
    private boolean truncated;

    synchronized boolean append(String part) {
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
        return truncated;
    }

    synchronized Snapshot snapshot() {
        return new Snapshot(text.toString(), truncated);
    }

    synchronized boolean truncated() {
        return truncated;
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
