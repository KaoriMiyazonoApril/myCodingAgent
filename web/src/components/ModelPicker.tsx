import { useId, useMemo, useState } from "react";

import type { ModelProfileView } from "../api";

type ModelPickerProps = {
  label: string;
  value: string;
  profiles: ModelProfileView[];
  disabled?: boolean;
  onChange: (modelId: string) => void;
};

export function ModelPicker({
  label,
  value,
  profiles,
  disabled = false,
  onChange,
}: ModelPickerProps) {
  const id = useId().replaceAll(":", "");
  const [draftQuery, setDraftQuery] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [customOverride, setCustomOverride] = useState<boolean | null>(null);
  const query = draftQuery ?? value;
  const custom = customOverride ?? (
    Boolean(value) && !profiles.some((profile) => profile.model_id === value)
  );

  const matches = useMemo(() => {
    const needle = query === value ? "" : query.trim().toLocaleLowerCase();
    return profiles.filter((profile) =>
      !needle ||
      profile.model_id.toLocaleLowerCase().includes(needle) ||
      profile.display_name.toLocaleLowerCase().includes(needle),
    );
  }, [profiles, query, value]);

  if (custom) {
    return (
      <div className="model-picker">
        <label htmlFor={`${id}-custom`}>{label}</label>
        <input
          id={`${id}-custom`}
          aria-label="自定义模型 ID"
          value={value}
          disabled={disabled}
          placeholder="输入 Custom Model ID"
          onChange={(event) => onChange(event.target.value)}
        />
        <button
          type="button"
          className="text-button"
          disabled={disabled || profiles.length === 0}
          onClick={() => {
            setCustomOverride(false);
            setDraftQuery(null);
          }}
        >
          返回模型列表
        </button>
      </div>
    );
  }

  return (
    <div className="model-picker">
      <label htmlFor={`${id}-search`}>{label}</label>
      <input
        id={`${id}-search`}
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls={`${id}-options`}
        value={query}
        disabled={disabled}
        placeholder="搜索模型"
        onFocus={() => setOpen(true)}
        onChange={(event) => {
          setDraftQuery(event.target.value);
          setOpen(true);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            setOpen(false);
          } else if (event.key === "Enter" && matches[0] !== undefined) {
            event.preventDefault();
            onChange(matches[0].model_id);
            setDraftQuery(null);
            setOpen(false);
          }
        }}
      />
      {open ? (
        <div className="model-picker-options" id={`${id}-options`} role="listbox">
          {matches.map((profile) => (
            <button
              type="button"
              role="option"
              aria-selected={profile.model_id === value}
              key={profile.model_id}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => {
                onChange(profile.model_id);
                setDraftQuery(null);
                setOpen(false);
              }}
            >
              <strong>{profile.display_name}</strong>
              <code>{profile.model_id}</code>
            </button>
          ))}
          {matches.length === 0 ? <p>没有匹配的已发现模型。</p> : null}
        </div>
      ) : null}
      <button
        type="button"
        className="text-button"
        disabled={disabled}
        onClick={() => {
          setOpen(false);
          setCustomOverride(true);
          if (profiles.some((profile) => profile.model_id === value)) {
            onChange("");
          }
        }}
      >
        使用 Custom Model ID
      </button>
    </div>
  );
}
