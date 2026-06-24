"use client";

// Structured, readable view of the requirements synthesis JSON produced by the
// REQUIREMENTS_SYNTHESIS prompt. The schema is fixed:
//   { app_name, purpose, target_users,
//     screens: [{ name, purpose, key_components[], user_actions[] }],
//     navigation: { type, main_tabs[], flows[] },
//     visual_requirements: { style_references, color_preferences, special_requirements } }
// Falls back gracefully when fields are missing or when the synthesizer returned
// a raw_requirements blob instead of structured data.

interface Screen {
  name?: string;
  purpose?: string;
  key_components?: string[];
  user_actions?: string[];
}

interface RequirementsDoc {
  app_name?: string;
  purpose?: string;
  target_users?: string;
  screens?: Screen[];
  navigation?: { type?: string; main_tabs?: string[]; flows?: string[] };
  visual_requirements?: {
    style_references?: string;
    color_preferences?: string;
    special_requirements?: string;
  };
  raw_requirements?: string;
}

function Chip({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-block px-2 py-0.5 text-xs rounded-full bg-gray-100 text-gray-700 border border-gray-200">
      {children}
    </span>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-semibold text-gray-800">{title}</h3>
      {children}
    </div>
  );
}

const NAV_TYPE_LABELS: Record<string, string> = {
  bottom_tabs: "하단 탭",
  drawer: "드로어",
  stack: "스택 내비게이션",
};

export function RequirementsView({ doc }: { doc: RequirementsDoc }) {
  // Synthesizer fallback: no structured fields, just raw text.
  if (doc.raw_requirements && !doc.screens?.length && !doc.purpose) {
    return (
      <div className="p-4 bg-gray-50 border rounded-mdesigner">
        <p className="text-xs text-gray-500 mb-2">구조화되지 않은 요구사항</p>
        <p className="text-sm text-gray-700 whitespace-pre-wrap">{doc.raw_requirements}</p>
      </div>
    );
  }

  const nav = doc.navigation || {};
  const visual = doc.visual_requirements || {};
  const hasVisual =
    visual.style_references || visual.color_preferences || visual.special_requirements;

  return (
    <div className="space-y-6" data-testid="requirements-view">
      {/* Header */}
      <div className="space-y-1">
        <h2 className="text-xl font-semibold text-gray-900">{doc.app_name || "앱 이름 미정"}</h2>
        {doc.purpose && <p className="text-sm text-gray-600">{doc.purpose}</p>}
        {doc.target_users && (
          <p className="text-sm text-gray-500">
            <span className="font-medium text-gray-600">대상 사용자: </span>
            {doc.target_users}
          </p>
        )}
      </div>

      {/* Navigation */}
      {(nav.type || nav.main_tabs?.length || nav.flows?.length) && (
        <Section title="내비게이션">
          <div className="space-y-2">
            {nav.type && (
              <div className="flex items-center gap-2 text-sm">
                <span className="text-gray-500">구조:</span>
                <Chip>{NAV_TYPE_LABELS[nav.type] || nav.type}</Chip>
              </div>
            )}
            {!!nav.main_tabs?.length && (
              <div className="flex items-center gap-2 flex-wrap text-sm">
                <span className="text-gray-500">메인 탭:</span>
                {nav.main_tabs.map((t, i) => (
                  <Chip key={i}>{t}</Chip>
                ))}
              </div>
            )}
            {!!nav.flows?.length && (
              <ul className="text-sm text-gray-600 space-y-1 mt-1">
                {nav.flows.map((f, i) => (
                  <li key={i} className="flex items-start gap-2">
                    <span className="text-gray-400">→</span>
                    <span>{f}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Section>
      )}

      {/* Screens */}
      {!!doc.screens?.length && (
        <Section title={`화면 (${doc.screens.length})`}>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {doc.screens.map((screen, i) => (
              <div key={i} className="p-3 border rounded-mdesigner bg-white space-y-2">
                <div>
                  <p className="text-sm font-medium text-gray-900">{screen.name || `화면 ${i + 1}`}</p>
                  {screen.purpose && <p className="text-xs text-gray-500 mt-0.5">{screen.purpose}</p>}
                </div>
                {!!screen.key_components?.length && (
                  <div className="flex flex-wrap gap-1">
                    {screen.key_components.map((c, j) => (
                      <Chip key={j}>{c}</Chip>
                    ))}
                  </div>
                )}
                {!!screen.user_actions?.length && (
                  <ul className="text-xs text-gray-600 space-y-0.5">
                    {screen.user_actions.map((a, j) => (
                      <li key={j} className="flex items-start gap-1.5">
                        <span className="text-primary">•</span>
                        <span>{a}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        </Section>
      )}

      {/* Visual requirements */}
      {hasVisual && (
        <Section title="비주얼 요구사항">
          <div className="text-sm text-gray-600 space-y-1">
            {visual.style_references && (
              <p><span className="text-gray-500">스타일 참고: </span>{visual.style_references}</p>
            )}
            {visual.color_preferences && (
              <p><span className="text-gray-500">색상 선호: </span>{visual.color_preferences}</p>
            )}
            {visual.special_requirements && (
              <p><span className="text-gray-500">특별 요구사항: </span>{visual.special_requirements}</p>
            )}
          </div>
        </Section>
      )}
    </div>
  );
}
