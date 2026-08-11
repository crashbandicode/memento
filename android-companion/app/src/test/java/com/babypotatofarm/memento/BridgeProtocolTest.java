package com.babypotatofarm.memento;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class BridgeProtocolTest {
    @Test
    public void acceptsBoundedSemanticHtml() {
        assertTrue(BridgeProtocol.htmlFitsClipboard("<p><b>Ready</b></p>"));
    }

    @Test
    public void rejectsEmptyAndOversizedHtml() {
        assertFalse(BridgeProtocol.htmlFitsClipboard(""));
        assertFalse(BridgeProtocol.htmlFitsClipboard("x".repeat(BridgeProtocol.MAX_HTML_BYTES + 1)));
    }
}
