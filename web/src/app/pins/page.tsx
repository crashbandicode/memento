"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, Pin } from "@/lib/api-client";
import { useI18n } from "@/lib/i18n";
import { Icon, ToolGlyph } from "@/components/aurora/Icon";
import { Btn, Glass, TopBar } from "@/components/aurora/primitives";

const PAGE_SIZE = 50;

export default function PinsPage() {
  const { t, locale } = useI18n();
  const [pins, setPins] = useState<Pin[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);

  useEffect(() => {
    let current = true;
    api.getPins(PAGE_SIZE)
      .then((response) => {
        if (!current) return;
        setPins(response.pins || []);
        setHasMore(response.has_more);
      })
      .catch((error: unknown) => {
        if (current) console.error("Failed to load pinned messages:", error);
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, []);

  const loadMore = async () => {
    if (loadingMore || !hasMore) return;
    setLoadingMore(true);
    try {
      const response = await api.getPins(PAGE_SIZE, pins.length);
      setPins((current) => [...current, ...(response.pins || [])]);
      setHasMore(response.has_more);
    } catch (error) {
      console.error("Failed to load more pinned messages:", error);
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <div data-pins-page className="max-w-4xl mx-auto">
      <TopBar title={t.nav.pins} subtitle={t.conversation.pinnedAcrossThreads} />
      {loading ? (
        <div style={{ padding: 32, textAlign: "center", color: "var(--aurora-fg4)" }}>
          {t.loading}
        </div>
      ) : pins.length === 0 ? (
        <Glass padding={20} radius={16}>
          <div style={{ display: "flex", alignItems: "center", gap: 9, color: "var(--aurora-fg3)", fontSize: 13 }}>
            <Icon name="target" size={16} />
            {t.conversation.noPinnedMessages}
          </div>
        </Glass>
      ) : (
        <div style={{ display: "grid", gap: 10 }}>
          {pins.map((pin) => {
            const document = pin.document;
            const message = pin.message;
            const conversationRef = pin.conversation_ref || pin.document_id;
            const href = `/conversations/${conversationRef}?line=${message?.line_number || 1}`;
            return (
              <div key={pin.id} data-pinned-message={pin.message_id}>
                <Glass hover padding={15} radius={15}>
                  <Link
                    href={href}
                    prefetch={false}
                    data-pin-conversation-link
                    style={{ display: "block", textDecoration: "none", color: "inherit" }}
                  >
                    <div style={{ display: "flex", minWidth: 0, alignItems: "center", gap: 9, marginBottom: 8 }}>
                      <ToolGlyph id={document?.tool_id || "codex"} size={24} />
                      <span style={{ minWidth: 0, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--aurora-fg1)", fontSize: 13, fontWeight: 650 }}>
                        {document?.title || conversationRef}
                      </span>
                      <span style={{ color: "var(--aurora-fg4)", fontSize: 10.5, whiteSpace: "nowrap" }}>
                        {document?.tool_id}
                      </span>
                    </div>
                    <div style={{ color: "var(--aurora-fg2)", fontSize: 13, lineHeight: 1.5, overflowWrap: "anywhere" }}>
                      {message?.snippet || "…"}
                    </div>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 7, marginTop: 9, color: "var(--aurora-fg4)", fontSize: 10.5 }}>
                      <span>{message?.role || t.conversation.pinnedMessage}</span>
                      {message?.line_number && <span>· {t.conversation.line} {message.line_number}</span>}
                      {message?.timestamp && <span>· {new Date(message.timestamp).toLocaleString(locale)}</span>}
                    </div>
                    {pin.note && (
                      <div style={{ marginTop: 8, padding: "6px 8px", borderRadius: 8, background: "var(--aurora-chip)", color: "var(--aurora-fg3)", fontSize: 11.5, overflowWrap: "anywhere" }}>
                        {pin.note}
                      </div>
                    )}
                  </Link>
                </Glass>
              </div>
            );
          })}
          {hasMore && (
            <div style={{ display: "flex", justifyContent: "center", marginTop: 4 }}>
              <Btn variant="glass" onClick={() => void loadMore()} disabled={loadingMore}>
                {loadingMore ? t.loading : t.conversation.loadEarlier}
              </Btn>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
