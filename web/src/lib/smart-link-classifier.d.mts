export type SmartLinkInfo =
  | { kind: "plain"; href: string; label: string }
  | { kind: "file"; href: string; label: string; path: string; domain?: string }
  | {
      kind: "git-compare";
      href: string;
      label: string;
      provider: "github" | "gitlab";
      refs: [string, string];
      domain: string;
    }
  | {
      kind: "git-commit";
      href: string;
      label: string;
      provider: "github" | "gitlab";
      ref: string;
      domain: string;
    }
  | { kind: "web"; href: string; label: string; domain: string }
  | {
      kind: "canvas";
      href: string;
      label: string;
      name: string;
      path: string;
      domain?: string;
    };

export type SmartCodeInfo =
  | { kind: "plain"; value: string }
  | { kind: "file"; value: string; display: string }
  | { kind: "sha"; value: string; display: string }
  | { kind: "canvas"; value: string; display: string; path: string };

export function looksLikeFilePath(value: string): boolean;
export function looksLikeDirectoryPath(value: string): boolean;
export function displayFileName(value: string): string;
export function classifySmartLink(href: string, label?: string): SmartLinkInfo;
export function classifyInlineCode(value: string): SmartCodeInfo;
