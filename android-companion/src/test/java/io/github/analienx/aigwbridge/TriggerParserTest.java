package io.github.analienx.aigwbridge;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

import org.junit.Test;

public final class TriggerParserTest {
    @Test
    public void explicitPhrasesAreParsed() {
        assertEquals("check the repo", TriggerParser.extractTask(
                "delegate locally: check the repo", 20_000));
        assertEquals("run tests", TriggerParser.extractTask(
                "Local delegate, run tests", 20_000));
        assertEquals("inspect logs", TriggerParser.extractTask(
                "delegate this to the local machine inspect logs", 20_000));
    }

    @Test
    public void ordinaryConversationAndResultsAreIgnored() {
        assertNull(TriggerParser.extractTask("tell me about local agents", 20_000));
        assertNull(TriggerParser.extractTask(
                "[local delegation result 12345678] done", 20_000));
    }

    @Test
    public void emptyOrOversizedTasksFailClosed() {
        assertNull(TriggerParser.extractTask("delegate locally", 20_000));
        assertNull(TriggerParser.extractTask("delegate locally abcdef", 3));
    }
}
