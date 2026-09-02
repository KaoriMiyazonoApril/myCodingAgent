import type { ModelProfileView } from "../api";

export function ModelProfileSummary({ profile }: { profile: ModelProfileView }) {
  const thinking = profile.thinking;
  const context = profile.context_window_tokens;
  const contextLabel =
    context === null || context === undefined
      ? "Context Window：未知"
      : `Context Window：${context.toLocaleString()} tokens`;
  const thinkingLabel =
    thinking?.supported !== true
      ? "Thinking：未知"
      : thinking.toggle_supported === false
        ? "Thinking：始终开启"
        : thinking.intensity_supported
          ? `Thinking：可切换 · ${thinking.intensity_options.join(" / ")}`
          : "Thinking：可切换";

  return (
    <div className="model-profile-summary" aria-label="模型能力摘要">
      <strong>{profile.display_name}</strong>
      {profile.description ? <p>{profile.description}</p> : null}
      <small>
        {contextLabel} · {thinkingLabel}
      </small>
    </div>
  );
}
