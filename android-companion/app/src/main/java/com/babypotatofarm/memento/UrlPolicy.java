package com.babypotatofarm.memento;

import java.net.URI;

final class UrlPolicy {
    static final String ORIGIN = "https://memento.babypotatofarm.com";
    static final String START_URL = ORIGIN + "/app";

    private UrlPolicy() {}

    static boolean isTrusted(String value) {
        try {
            URI uri = URI.create(value);
            int port = uri.getPort();
            return "https".equalsIgnoreCase(uri.getScheme())
                && "memento.babypotatofarm.com".equalsIgnoreCase(uri.getHost())
                && (port == -1 || port == 443)
                && uri.getUserInfo() == null;
        } catch (IllegalArgumentException ignored) {
            return false;
        }
    }
}
