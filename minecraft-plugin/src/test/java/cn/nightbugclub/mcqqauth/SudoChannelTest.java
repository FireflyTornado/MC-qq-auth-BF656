package cn.nightbugclub.mcqqauth;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

class SudoChannelTest {
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
