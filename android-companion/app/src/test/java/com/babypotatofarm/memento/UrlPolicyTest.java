package com.babypotatofarm.memento;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class UrlPolicyTest {
    @Test
    public void acceptsOnlyTheConfiguredHttpsOrigin() {
        assertTrue(UrlPolicy.isTrusted("https://memento.babypotatofarm.com/app"));
        assertTrue(UrlPolicy.isTrusted("https://memento.babypotatofarm.com:443/conversations/abc"));

        assertFalse(UrlPolicy.isTrusted("http://memento.babypotatofarm.com/app"));
        assertFalse(UrlPolicy.isTrusted("https://evil.memento.babypotatofarm.com/app"));
        assertFalse(UrlPolicy.isTrusted("https://memento.babypotatofarm.com:444/app"));
        assertFalse(UrlPolicy.isTrusted("https://user@memento.babypotatofarm.com/app"));
    }
}
