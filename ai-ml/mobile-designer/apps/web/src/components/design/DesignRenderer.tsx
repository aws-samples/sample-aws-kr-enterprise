"use client";

import { clsx } from "clsx";

// props/style come from an arbitrary AI-generated design JSON tree, so their
// values are intentionally untyped (any) — callers guard with String()/Boolean().
interface Component {
  id: string;
  type: string;
  props?: Record<string, any>;
  style?: Record<string, any>;
  children?: Component[];
}

interface Tokens {
  colors?: Record<string, string>;
  typography?: Record<string, { fontSize?: string; fontWeight?: string }>;
  spacing?: Record<string, string>;
}

interface DesignRendererProps {
  components: Component[];
  tokens?: Tokens;
  colorMap?: Record<string, string>;
  selectedId: string | null;
  hoveredId: string | null;
  onSelect: (id: string | null) => void;
  onHover: (id: string | null) => void;
  onNavigate?: (screenName: string) => void;
  darkMode?: boolean;
}

function px(dp: string | undefined): string | undefined {
  if (!dp) return undefined;
  const raw = dp.replace("dp", "").replace("sp", "").replace("px", "");
  const num = parseFloat(raw);
  if (isNaN(num)) return dp.replace("dp", "px").replace("sp", "px");
  return `${num}px`;
}

function resolveColor(color: string | undefined, colorMap?: Record<string, string>): string | undefined {
  if (!color || !colorMap) return color;
  const normalized = color.toLowerCase();
  return colorMap[normalized] || color;
}

function resolveStyle(style: Record<string, unknown> | undefined, colorMap?: Record<string, string>): Record<string, unknown> | undefined {
  if (!style || !colorMap) return style;
  const colorKeys = ["backgroundColor", "color", "borderColor", "textColor", "iconColor", "tintColor",
    "expandedTitleColor", "subtitleColor", "navigationIconColor", "actionIconColor",
    "selectedBackgroundColor", "unselectedBackgroundColor", "selectedTextColor", "unselectedTextColor",
    "selectedBorderColor", "unselectedBorderColor", "containerColor", "contentColor"];
  const resolved = { ...style };
  for (const key of colorKeys) {
    if (typeof resolved[key] === "string") {
      resolved[key] = resolveColor(resolved[key] as string, colorMap);
    }
  }
  return resolved;
}

function DesignNode({ comp, tokens, colorMap, selectedId, hoveredId, onSelect, onHover, onNavigate, darkMode }: {
  comp: Component;
  tokens: Tokens;
  colorMap?: Record<string, string>;
  selectedId: string | null;
  hoveredId: string | null;
  onSelect: (id: string | null) => void;
  onHover: (id: string | null) => void;
  onNavigate?: (screenName: string) => void;
  darkMode?: boolean;
}) {
  const isSelected = comp.id === selectedId;
  const isHovered = comp.id === hoveredId && !isSelected;
  const colors = tokens.colors || {};
  const primary = colors.primary || "#0381fe";
  const primaryDark = colors.primaryDark || colors.primaryVariant || "#0072de";
  const bg = darkMode ? "#080808" : (colors.background || "#fafafa");
  const surface = darkMode ? "#1a1a1a" : (colors.surface || "#ffffff");
  const surfaceContainer = darkMode ? "#2a2a2a" : (colors.surfaceContainer || colors.surfaceContainerLow || "#f3f4f6");
  const text = darkMode ? "#ffffff" : (colors.onSurface || colors.text || "#000000");
  const textSecondary = darkMode ? "#ffffff99" : (colors.onSurfaceVariant || colors.textSecondary || "#49454f");
  const divider = darkMode ? "#ffffff1f" : (colors.outline || colors.divider || "#e0e0e0");

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
    <DesignNode
      key={child.id}
      comp={child}
      tokens={tokens}
      colorMap={colorMap}
      selectedId={selectedId}
      hoveredId={hoveredId}
      onSelect={onSelect}
      onHover={onHover}
      onNavigate={onNavigate}
      darkMode={darkMode}
    />
  ));

  switch (comp.type) {
    case "TopAppBar":
    case "LargeTopAppBar":
      const appBarBg = (comp.style?.backgroundColor as string) || surface;
      const titleColor = (comp.style?.expandedTitleColor as string) || text;
      const isExtendTitle = comp.props?.extendTitle === true || comp.type === "LargeTopAppBar";
      const subtitleText = comp.props?.subtitle ? String(comp.props.subtitle) : "";
      const displayTitle = String(comp.props?.extendTitleText || comp.props?.title || "");
      const navIconColor = (comp.style?.navigationIconColor as string) || textSecondary;
      const actionIconColor = (comp.style?.actionIconColor as string) || textSecondary;
      return (
        <div className={clsx(wrapperClass)}
          style={{ backgroundColor: appBarBg, padding: "16px 24px" }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          <div className="flex items-center justify-between" style={{ height: "48px" }}>
            {comp.props?.navigationIcon && (
              <span className="material-icons-outlined flex items-center justify-center" style={{ color: navIconColor, fontSize: "24px", width: "48px", height: "48px" }}>
                {String(comp.props.navigationIcon)}
              </span>
            )}
            <div className="flex gap-0 ml-auto">
              {(comp.props?.actions as string[] || []).map((action, i) => (
                <span key={i} className="material-icons-outlined flex items-center justify-center" style={{ color: actionIconColor, fontSize: "24px", width: "48px", height: "48px" }}>
                  {action}
                </span>
              ))}
            </div>
          </div>
          {isExtendTitle && (
            <h1 style={{ fontSize: "28px", color: titleColor, fontWeight: 300, marginTop: "16px", marginBottom: "4px" }}>
              {displayTitle}
            </h1>
          )}
          {!isExtendTitle && displayTitle && (
            <span style={{ fontSize: "19px", fontWeight: 500, color: text }}>{displayTitle}</span>
          )}
          {subtitleText && (
            <p style={{ fontSize: "13px", color: (comp.style?.subtitleColor as string) || textSecondary, marginTop: "4px" }}>
              {subtitleText}
            </p>
          )}
        </div>
      );

    case "BottomNavigation":
      return (
        <div className={clsx(wrapperClass, "flex items-center justify-around py-3 border-t shrink-0")}
          style={{ backgroundColor: surface, borderColor: divider }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {(comp.children || []).map((item) => {
            const label = String(item.props?.label || item.props?.text || "");
            const iconName = String(item.props?.icon || "circle");
            const navigateTo = String(item.props?.navigate_to || item.props?.screen || label || "");
            const isActive = item.props?.selected === true;
            return (
              <div key={item.id} className="flex flex-col items-center gap-1 cursor-pointer"
                onClick={(e) => { e.stopPropagation(); if (onNavigate && navigateTo) onNavigate(navigateTo); }}>
                <span className="material-icons-outlined" style={{ fontSize: "24px", color: isActive ? primary : textSecondary }}>
                  {iconName}
                </span>
                <span style={{ fontSize: "11px", color: isActive ? primary : textSecondary, fontWeight: isActive ? 500 : 400 }}>
                  {label || "Tab"}
                </span>
              </div>
            );
          })}
        </div>
      );

    case "Card": {
      const cardBg = (comp.style?.backgroundColor as string) || surface;
      const cardRadius = px(comp.style?.cornerRadius as string) || "16px";
      const cardBorder = comp.style?.borderColor ? `1px solid ${comp.style.borderColor}` : undefined;
      const cardMarginH = px(comp.style?.marginHorizontal as string);
      const cardMarginV = px(comp.style?.marginVertical as string);
      return (
        <div className={clsx(wrapperClass, "p-4")}
          style={{
            backgroundColor: cardBg,
            borderRadius: cardRadius,
            border: cardBorder,
            marginLeft: cardMarginH,
            marginRight: cardMarginH,
            marginTop: cardMarginV,
            marginBottom: cardMarginV || px(comp.style?.marginBottom as string),
            boxShadow: comp.style?.elevation && comp.style.elevation !== "0dp" ? "0 2px 8px rgba(0,0,0,0.08)" : undefined,
          }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {children}
        </div>
      );
    }

    case "Button": {
      const btnBg = (comp.style?.backgroundColor as string) || primary;
      const btnColor = (comp.style?.textColor as string) || (comp.style?.contentColor as string) || "#ffffff";
      const btnRadius = px(comp.style?.cornerRadius as string) || px(comp.style?.borderRadius as string) || "18px";
      return (
        <button className={clsx(wrapperClass, "px-6 py-3 font-medium shadow-sm")}
          style={{ backgroundColor: btnBg, color: btnColor, fontSize: "15px", borderRadius: btnRadius }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {String(comp.props?.text || comp.props?.label || "Button")}
        </button>
      );
    }

    case "TextButton":
      return (
        <button className={clsx(wrapperClass, "px-3 py-1")}
          style={{ color: primaryDark, fontSize: "17px", background: "none", border: "none" }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {String(comp.props?.text || comp.props?.label || "Button")}
        </button>
      );

    case "IconButton": {
      const ibIcon = String(comp.props?.icon || comp.props?.name || "more_vert");
      const ibColor = (comp.style?.tintColor as string) || (comp.style?.iconColor as string) || textSecondary;
      return (
        <div className={clsx(wrapperClass, "w-10 h-10 rounded-full flex items-center justify-center")}
          style={{ backgroundColor: "transparent" }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          <span className="material-icons-outlined" style={{ fontSize: "24px", color: ibColor }}>{ibIcon}</span>
        </div>
      );
    }

    case "FAB":
    case "FloatingActionButton":
    case "ExtendedFloatingActionButton": {
      const fabBg = (comp.style?.backgroundColor as string) || (comp.style?.containerColor as string) || primary;
      const fabTextColor = (comp.style?.textColor as string) || (comp.style?.contentColor as string) || "#ffffff";
      const fabIconColor = (comp.style?.iconColor as string) || fabTextColor;
      const fabText = comp.props?.text || comp.props?.label;
      const fabIcon = comp.props?.icon || comp.props?.collapsedIcon || "add";
      const fabRadius = px(comp.style?.cornerRadius as string) || "16px";
      const fabFontSize = px(comp.style?.fontSize as string) || "14px";
      const fabFontWeight = comp.style?.fontWeight === "SemiBold" ? 600 : comp.style?.fontWeight === "Bold" ? 700 : 500;
      if (fabText) {
        return (
          <div className={clsx(wrapperClass, "px-5 py-4 shadow-md flex items-center gap-2 w-fit")}
            style={{ backgroundColor: fabBg, borderRadius: fabRadius }}
            onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
            <span className="material-icons-outlined" style={{ color: fabIconColor, fontSize: "20px" }}>{fabIcon}</span>
            <span style={{ color: fabTextColor, fontSize: fabFontSize, fontWeight: fabFontWeight }}>{String(fabText)}</span>
          </div>
        );
      }
      return (
        <div className={clsx(wrapperClass, "w-14 h-14 shadow-md flex items-center justify-center")}
          style={{ backgroundColor: fabBg, borderRadius: fabRadius }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          <span className="material-icons-outlined" style={{ color: fabIconColor, fontSize: "24px" }}>{fabIcon}</span>
        </div>
      );
    }

    case "SearchBar":
      return (
        <div className={clsx(wrapperClass, "mx-6 rounded-full px-4 py-3 flex items-center gap-3")}
          style={{ backgroundColor: (comp.style?.backgroundColor as string) || surfaceContainer, borderRadius: px(comp.style?.cornerRadius as string) || "28px" }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          <div className="w-5 h-5 rounded-full" style={{ backgroundColor: textSecondary }} />
          <span style={{ color: textSecondary, fontSize: "16px" }}>
            {String(comp.props?.placeholder || comp.props?.label || comp.props?.text || "검색")}
          </span>
          {comp.props?.trailingIcon && <div className="w-5 h-5 rounded-full ml-auto" style={{ backgroundColor: textSecondary }} />}
        </div>
      );

    case "TextField":
      return (
        <div className={clsx(wrapperClass, "mx-6 rounded-xl px-4 py-3 border")}
          style={{ borderColor: divider, backgroundColor: surface }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {comp.props?.label && <span style={{ color: textSecondary, fontSize: "12px" }}>{String(comp.props.label)}</span>}
          <span style={{ color: textSecondary, fontSize: "16px", display: "block", marginTop: comp.props?.label ? "4px" : "0" }}>
            {String(comp.props?.placeholder || comp.props?.text || "")}
          </span>
        </div>
      );

    case "ListItem": {
      const listBg = (comp.style?.backgroundColor as string) || undefined;
      const headline = comp.props?.headlineText || comp.props?.title || comp.props?.text || "List Item";
      const supporting = comp.props?.supportingText || comp.props?.subtitle || "";
      const trailing = comp.props?.trailingTopText || "";
      const padH = px(comp.style?.paddingHorizontal as string) || "24px";
      const padV = px(comp.style?.paddingVertical as string) || "14px";
      return (
        <div className={clsx(wrapperClass, "flex items-start gap-3 overflow-hidden")}
          style={{ backgroundColor: listBg, padding: `${padV} ${padH}`, minHeight: px(comp.style?.minHeight as string) || "64px" }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          <div className="rounded-full shrink-0 flex items-center justify-center"
            style={{ width: "40px", height: "40px", backgroundColor: colors.avatarBlue || (darkMode ? "#ffffff1a" : "#e0e0e0") }}>
            <span style={{ color: "#fff", fontSize: "14px", fontWeight: 600 }}>
              {String(headline).charAt(0)}
            </span>
          </div>
          <div className="flex-1 min-w-0 overflow-hidden">
            <div className="flex items-center justify-between gap-2">
              <span style={{ fontSize: "16px", color: text, fontWeight: comp.props?.isUnread ? 600 : 400 }} className="truncate">
                {String(headline)}
              </span>
              {trailing && <span style={{ fontSize: "12px", color: textSecondary }} className="shrink-0">{String(trailing)}</span>}
            </div>
            {supporting && (
              <div style={{ fontSize: "13px", color: textSecondary, marginTop: "2px" }} className="truncate">
                {String(supporting)}
              </div>
            )}
          </div>
          <div className="shrink-0 flex items-center gap-1">
            {comp.props?.isStarred && <div className="w-4 h-4 rounded-sm" style={{ backgroundColor: colors.starActive || "#FBBC04" }} />}
            {children}
          </div>
        </div>
      );
    }

    case "Chip":
    case "FilterChip": {
      const isChipSelected = comp.props?.selected === true;
      const chipBg = isChipSelected
        ? (comp.style?.selectedBackgroundColor as string) || primary
        : (comp.style?.unselectedBackgroundColor as string) || surface;
      const chipColor = isChipSelected
        ? (comp.style?.selectedTextColor as string) || "#ffffff"
        : (comp.style?.unselectedTextColor as string) || text;
      const chipBorder = isChipSelected
        ? (comp.style?.selectedBorderColor as string) || primary
        : (comp.style?.unselectedBorderColor as string) || divider;
      return (
        <span className={clsx(wrapperClass, "inline-flex items-center px-4 py-2 rounded-full border")}
          style={{ borderColor: chipBorder, fontSize: "14px", color: chipColor, backgroundColor: chipBg }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {String(comp.props?.text || comp.props?.label || "Chip")}
        </span>
      );
    }

    case "Divider": {
      const divColor = (comp.style?.color as string) || divider;
      const divMarginH = px(comp.style?.marginHorizontal as string) || "24px";
      return (
        <div className={wrapperClass}
          style={{ height: px(comp.style?.height as string) || "1px", backgroundColor: divColor, marginLeft: divMarginH, marginRight: divMarginH }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)} />
      );
    }

    case "Text": {
      const textNodeStyle: React.CSSProperties = {
        color: (comp.style?.color as string) || text,
        fontSize: px(comp.style?.fontSize as string) || "16px",
        fontWeight: comp.style?.fontWeight === "Bold" ? 700 : comp.style?.fontWeight === "SemiBold" ? 600 : comp.style?.fontWeight === "Medium" ? 500 : comp.style?.fontWeight === "Light" ? 300 : 400,
        letterSpacing: px(comp.style?.letterSpacing as string) || undefined,
        paddingLeft: px(comp.style?.paddingHorizontal as string) || undefined,
        paddingRight: px(comp.style?.paddingHorizontal as string) || undefined,
        paddingTop: px(comp.style?.paddingTop as string) || undefined,
        paddingBottom: px(comp.style?.paddingBottom as string) || undefined,
      };
      if (!comp.style?.fontSize) {
        const variant = comp.props?.variant || comp.props?.style;
        if (variant === "title" || variant === "headline") { textNodeStyle.fontSize = "19px"; textNodeStyle.fontWeight = 500; }
        else if (variant === "label" || variant === "caption") { textNodeStyle.fontSize = "13px"; textNodeStyle.color = textSecondary; }
        else if (variant === "extendTitle") { textNodeStyle.fontSize = "34px"; textNodeStyle.fontWeight = 300; }
      }
      return (
        <span className={clsx(wrapperClass, "block")} style={textNodeStyle}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {String(comp.props?.text || "")}
        </span>
      );
    }

    case "Icon": {
      const iconSize = px(comp.props?.size as string) || px(comp.style?.size as string) || "24px";
      const iconColor = (comp.style?.tintColor as string) || (comp.style?.color as string) || textSecondary;
      const iconName = String(comp.props?.icon || comp.props?.name || "circle");
      return (
        <span className={clsx(wrapperClass, "material-icons-outlined shrink-0")}
          style={{ fontSize: iconSize, color: iconColor }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {iconName}
        </span>
      );
    }

    case "Image":
      return (
        <div className={clsx(wrapperClass, "rounded-xl overflow-hidden")}
          style={{ backgroundColor: darkMode ? "#ffffff0f" : "#e0e0e0", height: "180px", borderRadius: "20px" }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          <div className="w-full h-full flex items-center justify-center">
            <span style={{ color: textSecondary, fontSize: "13px" }}>Image</span>
          </div>
        </div>
      );

    case "Switch":
      return (
        <div className={clsx(wrapperClass, "w-12 h-7 rounded-full relative")}
          style={{ backgroundColor: primary }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          <div className="absolute right-1 top-1 w-5 h-5 rounded-full bg-white" />
        </div>
      );

    case "Badge":
      return (
        <span className={clsx(wrapperClass, "inline-flex items-center px-2 py-0.5 rounded-full text-white")}
          style={{ backgroundColor: primary, fontSize: "11px" }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {String(comp.props?.text || "")}
        </span>
      );

    case "ProgressIndicator":
      return (
        <div className={clsx(wrapperClass, "h-1 rounded-full overflow-hidden")}
          style={{ backgroundColor: divider }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          <div className="h-full rounded-full" style={{ backgroundColor: primary, width: "60%" }} />
        </div>
      );

    case "Row":
      return (
        <div className={clsx(wrapperClass, "flex items-center gap-3 flex-wrap")}
          style={{
            backgroundColor: comp.style?.backgroundColor as string || undefined,
            padding: px(comp.style?.paddingHorizontal as string) ? `${px(comp.style?.paddingVertical as string) || "0"} ${px(comp.style?.paddingHorizontal as string)}` : undefined,
          }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {children}
        </div>
      );

    case "Column":
      return (
        <div className={clsx(wrapperClass, "flex flex-col gap-3")}
          style={{
            backgroundColor: comp.style?.backgroundColor as string || undefined,
            padding: px(comp.style?.padding as string) || undefined,
          }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {children}
        </div>
      );

    case "LazyColumn":
      return (
        <div className={clsx(wrapperClass, "flex flex-col gap-0 overflow-y-auto flex-1 px-4")}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {children}
        </div>
      );

    case "Spacer":
      return <div style={{ height: px(comp.style?.height as string) || "16px" }} />;

    case "Box":
      return (
        <div className={clsx(wrapperClass, "rounded-lg px-2 py-1")}
          style={{ backgroundColor: darkMode ? "#ffffff0f" : "#f5f5f5", borderRadius: "8px" }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {comp.props?.text && <span style={{ fontSize: "12px", color: text }}>{String(comp.props.text)}</span>}
          {children}
        </div>
      );

    case "Scaffold": {
      const topBar = comp.children?.find(c => c.type === "TopAppBar" || c.type === "LargeTopAppBar");
      const bottomNav = comp.children?.find(c => c.type === "BottomNavigation");
      const fab = comp.children?.find(c => c.type === "FAB" || c.type === "FloatingActionButton" || c.type === "ExtendedFloatingActionButton");
      const bodyChildren = comp.children?.filter(c =>
        c !== topBar && c !== bottomNav && c !== fab
      ) || [];

      return (
        <div className={clsx(wrapperClass, "flex flex-col h-full relative")} style={{ backgroundColor: bg }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {topBar && (
            <DesignNode comp={topBar} tokens={tokens} selectedId={selectedId} hoveredId={hoveredId}
              onSelect={onSelect} onHover={onHover} onNavigate={onNavigate} darkMode={darkMode} />
          )}
          <div className="flex-1 overflow-y-auto">
            {bodyChildren.map(child => (
              <DesignNode key={child.id} comp={child} tokens={tokens} selectedId={selectedId} hoveredId={hoveredId}
                onSelect={onSelect} onHover={onHover} onNavigate={onNavigate} darkMode={darkMode} />
            ))}
          </div>
          {fab && (
            <div className="absolute bottom-20 right-4 z-10">
              <DesignNode comp={fab} tokens={tokens} selectedId={selectedId} hoveredId={hoveredId}
                onSelect={onSelect} onHover={onHover} onNavigate={onNavigate} darkMode={darkMode} />
            </div>
          )}
          {bottomNav && (
            <DesignNode comp={bottomNav} tokens={tokens} selectedId={selectedId} hoveredId={hoveredId}
              onSelect={onSelect} onHover={onHover} onNavigate={onNavigate} darkMode={darkMode} />
          )}
        </div>
      );
    }

    case "Surface":
      return (
        <div className={clsx(wrapperClass)}
          style={{
            backgroundColor: (comp.style?.backgroundColor as string) || surfaceContainer,
            borderRadius: px(comp.style?.cornerRadius as string) || "12px",
            padding: px(comp.style?.padding as string) || "12px",
          }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {children}
        </div>
      );

    case "AssistChip":
      return (
        <span className={clsx(wrapperClass, "inline-flex items-center gap-1 px-3 py-1.5 rounded-full border")}
          style={{ borderColor: divider, fontSize: "13px", color: text, backgroundColor: surface }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          <div className="w-4 h-4 rounded-full" style={{ backgroundColor: textSecondary }} />
          {String(comp.props?.label || comp.props?.text || "Chip")}
        </span>
      );

    case "ModalNavigationDrawer":
    case "NavigationDrawer":
      return (
        <div className={clsx(wrapperClass, "flex flex-col h-full")}
          style={{ backgroundColor: surface }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {children}
        </div>
      );

    case "NavigationDrawerItem":
      const isDrawerSelected = comp.props?.selected === true;
      const drawerSelectedBg = colors.drawerSelectedIndicator || colors.primaryContainer || "#c2e7ff";
      const drawerSelectedText = colors.drawerSelectedText || colors.onPrimaryContainer || "#001d35";
      return (
        <div className={clsx(wrapperClass, "flex items-center gap-3 px-4 py-3 rounded-full mx-3 my-0.5")}
          style={{
            backgroundColor: isDrawerSelected ? drawerSelectedBg : "transparent",
            color: isDrawerSelected ? drawerSelectedText : text,
          }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          <div className="w-6 h-6 rounded-sm" style={{ backgroundColor: isDrawerSelected ? drawerSelectedText : textSecondary }} />
          <span style={{ fontSize: "14px", fontWeight: isDrawerSelected ? 600 : 400 }}>
            {String(comp.props?.label || comp.props?.text || "")}
          </span>
          {comp.props?.badge && (
            <span className="ml-auto text-xs" style={{ color: textSecondary }}>{String(comp.props.badge)}</span>
          )}
        </div>
      );

    case "SwipeToDismiss":
      return (
        <div className={clsx(wrapperClass)}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {children}
        </div>
      );

    default:
      return (
        <div className={clsx(wrapperClass)}
          style={{
            backgroundColor: comp.style?.backgroundColor as string || undefined,
            padding: px(comp.style?.padding as string) || "4px",
            borderRadius: px(comp.style?.cornerRadius as string) || undefined,
          }}
          onClick={handleClick} onMouseEnter={() => onHover(comp.id)} onMouseLeave={() => onHover(null)}>
          {comp.props?.text && <span style={{ fontSize: "14px", color: text }}>{String(comp.props.text)}</span>}
          {comp.props?.title && <span style={{ fontSize: "16px", color: text }}>{String(comp.props.title)}</span>}
          {children}
        </div>
      );
  }
}

function remapComponentColors(comp: Component, colorMap: Record<string, string>): Component {
  const colorKeys = ["backgroundColor", "color", "borderColor", "textColor", "iconColor", "tintColor",
    "expandedTitleColor", "subtitleColor", "navigationIconColor", "actionIconColor",
    "selectedBackgroundColor", "unselectedBackgroundColor", "selectedTextColor", "unselectedTextColor",
    "selectedBorderColor", "unselectedBorderColor", "containerColor", "contentColor"];

  let newStyle = comp.style;
  if (comp.style) {
    const s = { ...comp.style } as Record<string, unknown>;
    let changed = false;
    for (const key of colorKeys) {
      if (typeof s[key] === "string") {
        const resolved = colorMap[(s[key] as string).toLowerCase()];
        if (resolved) { s[key] = resolved; changed = true; }
      }
    }
    if (changed) newStyle = s as typeof comp.style;
  }

  const newChildren = comp.children?.map(c => remapComponentColors(c, colorMap));

  if (newStyle === comp.style && newChildren === comp.children) return comp;
  return { ...comp, style: newStyle, children: newChildren };
}

export function DesignRenderer({ components, tokens, colorMap, selectedId, hoveredId, onSelect, onHover, onNavigate, darkMode }: DesignRendererProps) {
  const resolvedTokens: Tokens = tokens || {};
  const resolvedComponents = colorMap
    ? components.map(c => remapComponentColors(c, colorMap))
    : components;

  return (
    <div className="flex flex-col h-full" style={{ backgroundColor: darkMode ? "#080808" : (resolvedTokens.colors?.background || "#fafafa") }} onClick={() => onSelect(null)}>
      {resolvedComponents.map((comp) => (
        <DesignNode
          key={comp.id}
          comp={comp}
          tokens={resolvedTokens}
          colorMap={colorMap}
          selectedId={selectedId}
          hoveredId={hoveredId}
          onSelect={onSelect}
          onHover={onHover}
          onNavigate={onNavigate}
          darkMode={darkMode}
        />
      ))}
      {resolvedComponents.length === 0 && (
        <p className="text-center text-gray-300 text-xs py-8">디자인을 생성하세요</p>
      )}
    </div>
  );
}
