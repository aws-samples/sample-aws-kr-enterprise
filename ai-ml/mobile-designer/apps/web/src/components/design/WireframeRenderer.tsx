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

interface WireframeRendererProps {
  components: Component[];
  selectedId: string | null;
  hoveredId: string | null;
  onSelect: (id: string | null) => void;
  onHover: (id: string | null) => void;
  onNavigate?: (screenName: string) => void;
}

function WireframeNode({ comp, selectedId, hoveredId, onSelect, onHover, onNavigate }: {
  comp: Component;
  selectedId: string | null;
  hoveredId: string | null;
  onSelect: (id: string | null) => void;
  onHover: (id: string | null) => void;
  onNavigate?: (screenName: string) => void;
}) {
  const isSelected = comp.id === selectedId;
  const isHovered = comp.id === hoveredId && !isSelected;

  const wrapperClass = clsx(
    "relative transition-all",
    isSelected && "ring-2 ring-blue-500 ring-offset-1",
    isHovered && "ring-1 ring-blue-300",
  );

  const handleClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    onSelect(isSelected ? null : comp.id);
  };

  const children = comp.children?.map((child) => (
    <WireframeNode
      key={child.id}
      comp={child}
      selectedId={selectedId}
      hoveredId={hoveredId}
      onSelect={onSelect}
      onHover={onHover}
      onNavigate={onNavigate}
    />
  ));

  switch (comp.type) {
    case "TopAppBar":
      return (
        <div className={clsx(wrapperClass, "flex items-center justify-between px-4 py-3 bg-gray-100 border-b border-gray-300")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          <div className="flex items-center gap-2">
            {comp.children?.filter(c => c.type === "IconButton").slice(0, 1).map(c => (
              <div key={c.id} className="w-6 h-6 bg-gray-400 rounded" />
            ))}
            <span className="text-sm font-medium text-gray-700">{String(comp.props?.title || "")}</span>
          </div>
          <div className="flex gap-2">
            {comp.children?.filter(c => c.type === "IconButton").slice(1).map(c => (
              <div key={c.id} className="w-6 h-6 bg-gray-400 rounded" />
            ))}
          </div>
        </div>
      );

    case "BottomNavigation":
      return (
        <div className={clsx(wrapperClass, "flex items-center justify-around py-2 bg-gray-100 border-t border-gray-300 mt-auto")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {(comp.children || []).map((item) => {
            const label = String(item.props?.label || item.props?.text || item.props?.title || "");
            const navigateTo = String(item.props?.navigate_to || item.props?.screen || label || "");
            return (
              <div key={item.id} className="flex flex-col items-center gap-1 cursor-pointer hover:opacity-70"
                onClick={(e) => { e.stopPropagation(); if (onNavigate && navigateTo) onNavigate(navigateTo); }}>
                <div className="w-5 h-5 bg-gray-400 rounded" />
                <span className="text-[9px] text-gray-500">{label || "Tab"}</span>
              </div>
            );
          })}
          {!comp.children?.length && Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="flex flex-col items-center gap-1">
              <div className="w-5 h-5 bg-gray-400 rounded" />
              <span className="text-[9px] text-gray-500">Tab</span>
            </div>
          ))}
        </div>
      );

    case "Card":
      return (
        <div className={clsx(wrapperClass, "border border-gray-300 rounded-lg p-3 bg-white")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {children}
        </div>
      );

    case "Button":
    case "TextButton":
      return (
        <div className={clsx(wrapperClass, "inline-block px-3 py-1.5 border border-gray-400 rounded text-xs text-gray-600 bg-gray-50")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {String(comp.props?.text || comp.props?.label || "Button")}
        </div>
      );

    case "TextField":
      return (
        <div className={clsx(wrapperClass, "border border-gray-300 rounded px-3 py-2 bg-white")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          <span className="text-xs text-gray-400">{String(comp.props?.placeholder || comp.props?.label || "Input")}</span>
        </div>
      );

    case "ListItem":
      return (
        <div className={clsx(wrapperClass, "flex items-center gap-3 px-4 py-3 border-b border-gray-200")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          <div className="w-8 h-8 bg-gray-300 rounded-full shrink-0" />
          <div className="flex-1">
            <div className="text-xs text-gray-700">{String(comp.props?.title || comp.props?.text || "List Item")}</div>
            {comp.props?.subtitle && <div className="text-[10px] text-gray-400">{String(comp.props.subtitle)}</div>}
          </div>
        </div>
      );

    case "Icon":
    case "IconButton":
      return (
        <div className={clsx(wrapperClass, "w-6 h-6 bg-gray-400 rounded shrink-0")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)} />
      );

    case "Text":
      return (
        <span className={clsx(wrapperClass, "text-xs text-gray-700 block")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {String(comp.props?.text || "")}
        </span>
      );

    case "Divider":
      return (
        <div className={wrapperClass} style={{ height: "1px", backgroundColor: "#d1d5db", margin: "4px 0" }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)} />
      );

    case "Spacer":
      return <div style={{ height: "8px" }} />;

    case "TabRow":
      return (
        <div className={clsx(wrapperClass, "flex border-b border-gray-300")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {comp.children?.map((tab) => (
            <div key={tab.id} className={clsx(
              "flex-1 text-center py-2 text-xs",
              tab.props?.selected ? "border-b-2 border-gray-700 font-medium text-gray-800" : "text-gray-500"
            )}>
              {String(tab.props?.label || tab.props?.text || "Tab")}
            </div>
          ))}
          {!comp.children?.length && <div className="flex-1 text-center py-2 text-xs text-gray-400">Tabs</div>}
        </div>
      );

    case "Tab":
      return (
        <div className={clsx(wrapperClass, "px-3 py-2 text-xs text-center", comp.props?.selected ? "font-medium text-gray-800 border-b-2 border-gray-700" : "text-gray-500")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {String(comp.props?.label || comp.props?.text || "Tab")}
        </div>
      );

    case "Chip":
    case "FilterChip":
    case "AssistChip":
      return (
        <span className={clsx(wrapperClass, "inline-block px-2 py-1 border border-gray-300 rounded-full text-[10px] text-gray-600")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {String(comp.props?.text || comp.props?.label || "Chip")}
        </span>
      );

    case "Row":
      return (
        <div className={clsx(wrapperClass, "flex items-center gap-2 flex-wrap")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {children}
        </div>
      );

    case "Column":
      return (
        <div className={clsx(wrapperClass, "flex flex-col gap-2")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {children}
        </div>
      );

    case "LazyColumn":
      return (
        <div className={clsx(wrapperClass, "flex flex-col gap-2 overflow-y-auto flex-1")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {children}
        </div>
      );

    case "Box":
      return (
        <div className={clsx(wrapperClass, "bg-gray-200 rounded px-2 py-0.5")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {comp.props?.text && <span className="text-[10px] text-gray-600">{String(comp.props.text)}</span>}
          {children}
        </div>
      );

    default:
      return (
        <div className={clsx(wrapperClass, "border border-dashed border-gray-300 rounded p-2")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          <div className="text-[9px] text-gray-400 mb-1">{comp.type}</div>
          {comp.props?.text && <span className="text-xs text-gray-600">{String(comp.props.text)}</span>}
          {children}
        </div>
      );
  }
}

export function WireframeRenderer({ components, selectedId, hoveredId, onSelect, onHover, onNavigate }: WireframeRendererProps) {
  return (
    <div className="flex flex-col h-full bg-white" onClick={() => onSelect(null)}>
      {components.map((comp) => (
        <WireframeNode
          key={comp.id}
          comp={comp}
          selectedId={selectedId}
          hoveredId={hoveredId}
          onSelect={onSelect}
          onHover={onHover}
          onNavigate={onNavigate}
        />
      ))}
      {components.length === 0 && (
        <p className="text-center text-gray-300 text-xs py-8">디자인을 생성하세요</p>
      )}
    </div>
  );
}
