package cn.nightbugclub.mcqqauth;

import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.core.Appender;
import org.apache.logging.log4j.core.LogEvent;
import org.apache.logging.log4j.core.LoggerContext;
import org.apache.logging.log4j.core.appender.AbstractAppender;
import org.apache.logging.log4j.core.appender.ConsoleAppender;
import org.apache.logging.log4j.core.filter.AbstractFilter;

final class ConsoleLogCapture extends AbstractFilter implements AutoCloseable {
    private static final String TERMINAL_APPENDER = "net.minecrell.terminalconsole.TerminalConsoleAppender";

    private final AbstractAppender console;
    private final Thread dispatchThread = Thread.currentThread();
    private final BoundedCommandOutput output;

    private ConsoleLogCapture(AbstractAppender console, BoundedCommandOutput output) {
        this.console = console;
        this.output = output;
    }

    static ConsoleLogCapture open(ClassLoader serverLoader, BoundedCommandOutput output) {
        if (!(LogManager.getContext(serverLoader, false) instanceof LoggerContext context)) {
            return null;
        }
        var appenders = context.getConfiguration().getRootLogger().getAppenders();
        // Mohist routes to both a terminal and a GUI queue. Observe one terminal sink.
        Appender terminal = appenders.get("Console");
        if (!isTerminal(terminal)) {
            terminal = appenders.values().stream().filter(ConsoleLogCapture::isTerminal).findFirst().orElse(null);
        }
        if (!(terminal instanceof AbstractAppender appender)) {
            return null;
        }

        var capture = new ConsoleLogCapture(appender, output);
        capture.start();
        appender.addFilter(capture);
        return capture;
    }

    private static boolean isTerminal(Appender appender) {
        return appender instanceof ConsoleAppender
                || appender != null && TERMINAL_APPENDER.equals(appender.getClass().getName());
    }

    @Override
    public Result filter(LogEvent event) {
        if (Thread.currentThread() == dispatchThread && output.acceptingOutput()) {
            output.appendLine(event.getMessage().getFormattedMessage());
        }
        return Result.NEUTRAL;
    }

    @Override
    public void close() {
        console.removeFilter(this);
        stop();
    }
}
