import { useEffect } from "react";

import type { ThreadSkills } from "../api";

type SkillsPopoverProps = {
  skills: ThreadSkills | undefined;
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
};

function activationLabel(value: string | undefined): string {
  return value === "explicit"
    ? "用户显式加载"
    : value === "tool"
      ? "模型通过 skill() 加载"
      : "加载来源未知";
}

function placementLabel(value: string | undefined): string {
  return value === "working_tail"
    ? "本轮 Working Context"
    : value === "tool_history"
      ? "Tool History"
      : "Context 位置未知";
}

function sourceLabel(value: string): string {
  return value.startsWith("~/") ? `用户 Skill · ${value}` : `项目 Skill · ${value}`;
}

export function SkillsPopover({ skills, open, onToggle, onClose }: SkillsPopoverProps) {
  const value: ThreadSkills = skills ?? {
    schema_version: 1,
    available: [],
    loaded: [],
    diagnostics: [],
  };

  useEffect(() => {
    if (!open) {
      return;
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose, open]);

  return (
    <div className="skills-popover-wrap">
      <button
        type="button"
        className="quiet-button skills-toggle"
        aria-label="技能"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={onToggle}
      >
        技能 {value.loaded.length} / {value.loaded.length + value.available.length}
      </button>
      {open ? (
        <div className="skills-popover" role="dialog" aria-label="技能">
          <div className="skills-popover-heading">
            <div>
              <p className="step-label">运行上下文</p>
              <h4>技能</h4>
            </div>
            <button type="button" className="icon-button" aria-label="关闭技能" onClick={onClose}>×</button>
          </div>
          <p className="skills-explainer">
            “可用”表示模型知道 Skill 名称和用途；“已加载”表示完整 SKILL.md 已进入本轮 Context。
          </p>
          <section aria-labelledby="loaded-skills-heading">
            <h5 id="loaded-skills-heading">已加载技能（{value.loaded.length}）</h5>
            {value.loaded.length > 0 ? value.loaded.map((skill) => (
              <article className="skill-popover-item" key={`loaded-${skill.name}`}>
                <strong>{skill.name}</strong>
                <small>{activationLabel(skill.activation_source)} · {placementLabel(skill.placement)}</small>
                <small>{sourceLabel(skill.source)}</small>
                <code>{skill.source_path}</code>
                <p>{skill.description}</p>
              </article>
            )) : <p className="thread-empty">本轮尚未加载技能。</p>}
          </section>
          <section aria-labelledby="available-skills-heading">
            <h5 id="available-skills-heading">可用技能（{value.available.length}）</h5>
            {value.available.length > 0 ? value.available.map((skill) => (
              <article className="skill-popover-item" key={`available-${skill.name}`}>
                <strong>{skill.name}</strong>
                <small>仅 Catalog 已发现 · 完整 Skill 尚未加载</small>
                <small>{sourceLabel(skill.source)}</small>
                <code>{skill.source_path}</code>
                <p>{skill.description}</p>
              </article>
            )) : <p className="thread-empty">尚未发现技能。</p>}
          </section>
        </div>
      ) : null}
    </div>
  );
}
