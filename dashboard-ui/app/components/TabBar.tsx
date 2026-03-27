"use client";
import type { TabId } from "../types";

const TABS: { id: TabId; label: string }[] = [
  { id: "home",         label: "Home" },
  { id: "applications", label: "Applications" },
  { id: "compliance",   label: "Compliance" },
  { id: "agents",       label: "Agents" },
  { id: "events",       label: "Event Explorer" },
  { id: "global",       label: "Global Store" },
  { id: "pipeline",     label: "Run Pipeline" },
  { id: "demo",         label: "Demo Suite" },
];

interface Props {
  active: TabId;
  onChange: (id: TabId) => void;
}

export default function TabBar({ active, onChange }: Props) {
  return (
    <div className="flex gap-1 overflow-x-auto pb-0.5 border-b border-gray-800 mb-6 shrink-0">
      {TABS.map((t) => (
        <button
          key={t.id}
          onClick={() => onChange(t.id)}
          className={`px-4 py-2 text-xs tracking-wider uppercase whitespace-nowrap rounded-t transition-colors ${
            active === t.id
              ? "bg-indigo-600 text-white"
              : "text-gray-500 hover:text-gray-300 hover:bg-gray-800"
          }`}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}
