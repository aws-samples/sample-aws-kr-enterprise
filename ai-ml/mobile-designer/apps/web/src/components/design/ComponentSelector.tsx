"use client";

import { clsx } from "clsx";

// props/style come from arbitrary AI-generated design JSON, so values are untyped.
interface Component {
  id: string;
  type: string;
  props?: Record<string, any>;
  style?: Record<string, any>;
  children?: Component[];
}

interface ComponentSelectorProps {
  components: Component[];
  selectedId: string | null;
  hoveredId: string | null;
  onSelect: (id: string | null) => void;
  onHover: (id: string | null) => void;
}

export function ComponentSelector({ components, selectedId, hoveredId, onSelect, onHover }: ComponentSelectorProps) {
  const renderComponent = (comp: Component) => (
    <div
      key={comp.id}
      className={clsx(
        "relative p-2 border rounded transition-all cursor-pointer",
        comp.id === selectedId && "border-primary ring-2 ring-primary/30",
        comp.id === hoveredId && comp.id !== selectedId && "border-blue-300 bg-blue-50/50",
        comp.id !== selectedId && comp.id !== hoveredId && "border-transparent",
      )}
      onClick={(e) => { e.stopPropagation(); onSelect(comp.id === selectedId ? null : comp.id); }}
      onMouseEnter={() => onHover(comp.id)}
      onMouseLeave={() => onHover(null)}
      data-testid={`component-${comp.id}`}
      data-component-id={comp.id}
    >
      <div className="text-xs text-gray-400 mb-1">{comp.type}</div>
      {comp.props?.text && <span className="text-sm">{String(comp.props.text)}</span>}
      {comp.children?.map(renderComponent)}
    </div>
  );

  return (
    <div className="space-y-1 py-2" onClick={() => onSelect(null)}>
      {components.map(renderComponent)}
      {components.length === 0 && <p className="text-center text-gray-300 text-xs py-8">디자인을 생성하세요</p>}
    </div>
  );
}
