import { useT, type Key } from "../../i18n";
import { tr } from "../../i18n/tr";
import { exportUrl } from "../../api/http";
import { EXAMPLES } from "../../lib/examples";
import { usePlayback } from "../../store/playbackStore";
import { useRunStore } from "../../store/runStore";
import { useUiStore } from "../../store/uiStore";

export function TopBar(): React.ReactElement {
  const t = useT();
  const status = useRunStore((s) => s.status);
  const source = useRunStore((s) => s.source);
  const runSource = useRunStore((s) => s.runSource);
  const finished = useRunStore((s) => s.finished);
  const runId = useRunStore((s) => s.runId);
  const setPresenting = useUiStore((s) => s.setPresenting);
  const { start, stop, loadExample } = useRunStore.getState();
  const { lang, setLang, settings, patchSettings, showSettings, toggleSettings } = useUiStore();
  const busy = status === "starting" || status === "running";
  const stale = runSource !== null && runSource !== source && status !== "idle";

  return (
    <header className="flex items-center gap-3 px-3 h-12 border-b border-[var(--border)] bg-[var(--panel)] relative">
      <strong className="mr-2">🔍 {t("app.title")}</strong>
      <select
        className="gb-input"
        aria-label={t("top.examples")}
        value=""
        onChange={(e) => {
          const ex = EXAMPLES.find((x) => x.name === e.target.value);
          if (ex) {
            loadExample(ex.name, ex.source);
            usePlayback.getState().reset();
          }
        }}
      >
        <option value="">{t("top.examples")}…</option>
        {EXAMPLES.map((x) => {
          const k = `ex.${x.name.replace(/\.py$/, "")}` as Key;
          const known = k in tr;
          return (<option key={x.name} value={x.name} title={known ? t(`${k}.desc` as Key) : undefined}>{known ? `${t(k)} (${x.name})` : x.name}</option>);
        })}
      </select>
      {busy ? (
        <button className="gb-btn gb-btn-danger" onClick={() => stop()}>■ {t("top.stop")}</button>
      ) : (
        <button className="gb-btn gb-btn-primary" disabled={!source.trim()} onClick={() => void start()}>▶ {t("top.run")}</button>
      )}
      <span className="text-sm opacity-80" aria-live="polite">
        {status === "running" && t("status.running")}
        {status === "finished" && finished && t("status.finished", { status: finished.status, steps: finished.steps })}
      </span>
      {stale && <span className="gb-chip gb-conf-suspect">⚠ {t("top.stale")}</span>}
      <span className="flex-1" />
      <details className="relative" aria-label={t("export.menu")}>
        <summary className={`gb-btn inline-flex items-center ${status === "finished" ? "" : "opacity-40 pointer-events-none"}`}>⇩ {t("export.menu")}</summary>
        {runId && status === "finished" && (
          <div className="gb-popover" style={{ right: 0, top: 32, width: 230 }}>
            <a className="gb-link" href={exportUrl(runId, "html")} download>{t("export.html")}</a>
            <a className="gb-link" href={exportUrl(runId, "svg")} download>{t("export.svg")}</a>
            <a className="gb-link" href={exportUrl(runId, "mermaid")} download>{t("export.mermaid")}</a>
          </div>
        )}
      </details>
      <button className="gb-btn" disabled={status !== "finished"} onClick={() => setPresenting(true)}>🎤 {t("present.enter")}</button>
      <button className="gb-btn" onClick={toggleSettings} aria-expanded={showSettings}>⚙ {t("top.settings")}</button>
      <button className="gb-btn" onClick={() => setLang(lang === "tr" ? "en" : "tr")}>{t("lang.switch")}</button>
      {showSettings && (
        <div className="gb-popover" role="dialog" aria-label={t("top.settings")}>
          <label>{t("set.maxSteps")}
            <input className="gb-input" type="number" min={1} max={200000} value={settings.maxSteps} onChange={(e) => patchSettings({ maxSteps: Number(e.target.value) })} />
          </label>
          <label>{t("set.timeout")}
            <input className="gb-input" type="number" min={1} max={60} value={settings.timeoutS} onChange={(e) => patchSettings({ timeoutS: Number(e.target.value) })} />
          </label>
          <label>{t("set.seed")}
            <input className="gb-input" type="number" value={settings.seed} onChange={(e) => patchSettings({ seed: Number(e.target.value) })} />
          </label>
          <label>{t("set.filter")}
            <select className="gb-input" value={settings.filter} onChange={(e) => patchSettings({ filter: e.target.value as typeof settings.filter })}>
              {(["user_only", "boundaries", "boundaries_verbose"] as const).map((f) => (<option key={f} value={f}>{t(`filter.${f}`)}</option>))}
            </select>
          </label>
          <label>{t("set.stdin")}
            <textarea className="gb-input" rows={3} value={settings.stdin} onChange={(e) => patchSettings({ stdin: e.target.value })} />
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={settings.inlineValues} onChange={(e) => patchSettings({ inlineValues: e.target.checked })} />
            {t("set.inline")}
          </label>
        </div>
      )}
    </header>
  );
}
