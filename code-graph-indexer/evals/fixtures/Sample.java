package evals.fixtures;

import java.util.logging.Logger;

/** Small Java fixture for indexer smoke tests. */
public class Sample {
    private static final Logger logger = Logger.getLogger(Sample.class.getName());

    /** Public entry point; calls the private helper. */
    public String greet(String name) {
        return formatMessage(name);
    }

    private String formatMessage(String name) {
        try {
            return "Hello, " + name + "!";
        } catch (RuntimeException e) {
            logger.warning("format failed: " + e);
            throw e;
        }
    }

    public Integer silentDivide(int a, int b) {
        try {
            return a / b;
        } catch (ArithmeticException e) {
            return null;
        }
    }
}
