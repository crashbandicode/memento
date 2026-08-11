package com.babypotatofarm.memento;

import android.content.ClipData;
import android.content.ClipDescription;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PersistableBundle;
import android.text.Html;
import android.text.Spanned;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.activity.ComponentActivity;
import androidx.activity.OnBackPressedCallback;
import androidx.webkit.WebMessageCompat;
import androidx.webkit.WebSettingsCompat;
import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.Collections;

public final class MainActivity extends ComponentActivity {
    private WebView webView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        webView = new WebView(this);
        webView.setLayoutParams(new ViewGroup.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.MATCH_PARENT
        ));
        setContentView(webView);

        configureWebView();
        installClipboardBridge();
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                if (webView.canGoBack()) {
                    webView.goBack();
                } else {
                    setEnabled(false);
                    getOnBackPressedDispatcher().onBackPressed();
                }
            }
        });

        if (savedInstanceState == null || webView.restoreState(savedInstanceState) == null) {
            webView.loadUrl(UrlPolicy.START_URL);
        }
    }

    private void configureWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setSupportMultipleWindows(false);
        settings.setMediaPlaybackRequiresUserGesture(true);
        settings.setUserAgentString(settings.getUserAgentString() + " MementoAndroid/0.1.0");

        if (WebViewFeature.isFeatureSupported(WebViewFeature.SAFE_BROWSING_ENABLE)) {
            WebSettingsCompat.setSafeBrowsingEnabled(settings, true);
        }

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(webView, false);

        boolean debuggable = (getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0;
        WebView.setWebContentsDebuggingEnabled(debuggable);
        webView.setWebChromeClient(new WebChromeClient());
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String target = request.getUrl().toString();
                if (request.isForMainFrame() && UrlPolicy.isTrusted(target)) {
                    return false;
                }
                if (request.isForMainFrame()) {
                    openExternal(request.getUrl());
                }
                return true;
            }
        });
    }

    private void installClipboardBridge() {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            return;
        }

        WebViewCompat.addWebMessageListener(
            webView,
            BridgeProtocol.OBJECT_NAME,
            Collections.singleton(UrlPolicy.ORIGIN),
            (view, message, sourceOrigin, isMainFrame, replyProxy) -> {
                if (!isMainFrame || !UrlPolicy.isTrusted(sourceOrigin.toString())) {
                    replyProxy.postMessage(errorReply(null, "untrusted_origin"));
                    return;
                }
                if (message.getType() != WebMessageCompat.TYPE_STRING) {
                    replyProxy.postMessage(errorReply(null, "invalid_message_type"));
                    return;
                }
                replyProxy.postMessage(handleBridgeMessage(message.getData()));
            }
        );
    }

    private String handleBridgeMessage(String raw) {
        String requestId = null;
        try {
            JSONObject request = new JSONObject(raw);
            requestId = request.optString("requestId", null);
            if (!BridgeProtocol.COPY_TYPE.equals(request.optString("type"))) {
                return errorReply(requestId, "unsupported_request");
            }

            String html = request.optString("html", "");
            String plain = request.optString("plain", "");
            if (plain.isBlank()) {
                return errorReply(requestId, "empty_content");
            }

            ClipboardManager clipboard = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            boolean copiedRich = BridgeProtocol.htmlFitsClipboard(html);
            ClipData clip;
            if (copiedRich) {
                Spanned styled = Html.fromHtml(html, Html.FROM_HTML_MODE_LEGACY);
                ClipDescription description = new ClipDescription(
                    "Memento message",
                    new String[] {
                        ClipDescription.MIMETYPE_TEXT_HTML,
                        ClipDescription.MIMETYPE_TEXT_PLAIN
                    }
                );
                clip = new ClipData(description, new ClipData.Item(styled, html, null, null));
            } else {
                clip = ClipData.newPlainText("Memento message", plain);
            }
            markSensitive(clip);
            clipboard.setPrimaryClip(clip);

            if (Build.VERSION.SDK_INT <= Build.VERSION_CODES.S_V2) {
                Toast.makeText(this, copiedRich ? "Formatted message copied" : "Message copied", Toast.LENGTH_SHORT).show();
            }
            return successReply(requestId, copiedRich ? "html" : "plain");
        } catch (JSONException | RuntimeException error) {
            return errorReply(requestId, "copy_failed");
        }
    }

    private static void markSensitive(ClipData clip) {
        PersistableBundle extras = new PersistableBundle();
        String key = Build.VERSION.SDK_INT >= 33
            ? ClipDescription.EXTRA_IS_SENSITIVE
            : "android.content.extra.IS_SENSITIVE";
        extras.putBoolean(key, true);
        clip.getDescription().setExtras(extras);
    }

    private static String successReply(String requestId, String format) {
        try {
            return new JSONObject()
                .put("requestId", requestId)
                .put("ok", true)
                .put("format", format)
                .toString();
        } catch (JSONException impossible) {
            return "{\"ok\":false,\"error\":\"reply_failed\"}";
        }
    }

    private static String errorReply(String requestId, String error) {
        try {
            return new JSONObject()
                .put("requestId", requestId)
                .put("ok", false)
                .put("error", error)
                .toString();
        } catch (JSONException impossible) {
            return "{\"ok\":false,\"error\":\"reply_failed\"}";
        }
    }

    private void openExternal(@NonNull Uri uri) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (RuntimeException ignored) {
            Toast.makeText(this, "No app can open this link", Toast.LENGTH_SHORT).show();
        }
    }

    @Override
    protected void onSaveInstanceState(@NonNull Bundle outState) {
        webView.saveState(outState);
        super.onSaveInstanceState(outState);
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.stopLoading();
            webView.setWebChromeClient(null);
            webView.setWebViewClient(null);
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}
