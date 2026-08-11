package com.babypotatofarm.memento;

import java.nio.charset.StandardCharsets;

final class BridgeProtocol {
    static final String OBJECT_NAME = "MementoAndroidClipboard";
    static final String COPY_TYPE = "copy-rich-clipboard";
    static final int MAX_HTML_BYTES = 750_000;

    private BridgeProtocol() {}

    static boolean htmlFitsClipboard(String html) {
        return html != null && !html.isBlank()
            && html.getBytes(StandardCharsets.UTF_8).length <= MAX_HTML_BYTES;
    }
}
