"use client";

import { MarkdownExportForm } from "@/components/conversations/MarkdownExportForm";
import { Glass, TopBar } from "@/components/aurora/primitives";

export default function ConversationExportPage() {
  return (
    <div className="max-w-4xl mx-auto">
      <TopBar
        title="Markdown export"
        subtitle="Create portable conversation archives that preserve the structure you see in Memento."
      />
      <Glass padding="clamp(16px, 3vw, 24px)" radius={22}>
        <MarkdownExportForm />
      </Glass>
    </div>
  );
}
