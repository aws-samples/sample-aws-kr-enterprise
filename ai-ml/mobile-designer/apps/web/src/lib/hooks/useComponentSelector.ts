"use client";

import { useCallback, useState } from "react";

interface ComponentSelection {
  selectedId: string | null;
  hoveredId: string | null;
}

export function useComponentSelector() {
  const [selection, setSelection] = useState<ComponentSelection>({ selectedId: null, hoveredId: null });

  const select = useCallback((componentId: string | null) => {
    setSelection((s) => ({ ...s, selectedId: componentId }));
  }, []);

  const hover = useCallback((componentId: string | null) => {
    setSelection((s) => ({ ...s, hoveredId: componentId }));
  }, []);

  const clear = useCallback(() => {
    setSelection({ selectedId: null, hoveredId: null });
  }, []);

  return { ...selection, select, hover, clear };
}
