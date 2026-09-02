import * as React from "react";
import { zh, type Dict } from "./zh";
import { en } from "./en";

export type { Dict } from "./zh";

export type Language = "zh" | "en";

/** 双语文案数据(InfoHint / 状态映射等数据模块用),组件侧经 tl() 取当前语言。 */
export interface LocalizedText {
  zh: string;
  en: string;
}

const DICTS: Record<Language, Dict> = { zh, en };
const FALLBACK: Language = "zh";

const STORAGE_KEY = "finboard-lang";

export const DOCUMENT_TITLES: Record<Language, string> = {
  zh: "FinDashboard — 交易控制台",
  en: "FinDashboard — Trading Console",
};

export function documentLang(lang: Language): string {
  return lang === "zh" ? "zh-CN" : "en";
}

export function readStoredLanguage(): Language {
  if (typeof window === "undefined") return FALLBACK;
  try {
    return localStorage.getItem(STORAGE_KEY) === "en" ? "en" : FALLBACK;
  } catch {
    return FALLBACK;
  }
}

interface I18nContextValue {
  lang: Language;
  setLanguage: (lang: Language) => void;
}

// 缺省中文且 setLanguage 为 no-op:既有测试/独立渲染组件无需包 Provider 也能工作。
const I18nContext = React.createContext<I18nContextValue>({
  lang: FALLBACK,
  setLanguage: () => {},
});

export function LanguageProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLangState] = React.useState<Language>(readStoredLanguage);

  React.useEffect(() => {
    document.documentElement.lang = documentLang(lang);
    document.title = DOCUMENT_TITLES[lang];
    try {
      localStorage.setItem(STORAGE_KEY, lang);
    } catch {
      /* localStorage 不可用时仅内存生效 */
    }
  }, [lang]);

  const setLanguage = React.useCallback((next: Language) => setLangState(next), []);
  const value = React.useMemo(() => ({ lang, setLanguage }), [lang, setLanguage]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useLanguage() {
  return React.useContext(I18nContext);
}

function lookup(dict: Dict, path: string): string | undefined {
  let node: unknown = dict;
  for (const seg of path.split(".")) {
    if (node && typeof node === "object" && seg in (node as Record<string, unknown>)) {
      node = (node as Record<string, unknown>)[seg];
    } else {
      return undefined;
    }
  }
  return typeof node === "string" ? node : undefined;
}

function interpolate(template: string, params?: Record<string, string | number>): string {
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (match, key: string) =>
    key in params ? String(params[key]) : match,
  );
}

/**
 * 取词 hook: t("settings.title", { n: 3 }) 按点路径查当前语言字典,
 * 缺 key 回退中文,再缺回退路径本身;tl() 取 LocalizedText 的当前语言值。
 */
export function useT() {
  const { lang } = React.useContext(I18nContext);
  const t = React.useCallback(
    (path: string, params?: Record<string, string | number>): string => {
      const raw = lookup(DICTS[lang], path) ?? lookup(DICTS[FALLBACK], path) ?? path;
      return interpolate(raw, params);
    },
    [lang],
  );
  const tl = React.useCallback(
    (text: LocalizedText): string => text[lang] ?? text[FALLBACK],
    [lang],
  );
  return { t, tl, lang };
}
