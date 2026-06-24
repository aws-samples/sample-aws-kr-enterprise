import { renderHook, act } from "@testing-library/react";
import { useDesignTokens } from "@/lib/hooks/useDesignTokens";

describe("useDesignTokens", () => {
  it("initializes with default tokens", () => {
    const { result } = renderHook(() => useDesignTokens());
    expect(result.current.tokens.colors.primary).toBe("#1a73e8");
    expect(result.current.isDirty).toBe(false);
  });

  it("updates a token and marks dirty", () => {
    const { result } = renderHook(() => useDesignTokens());
    act(() => {
      result.current.updateToken("colors", "primary", "#ff0000");
    });
    expect(result.current.isDirty).toBe(true);
    expect(result.current.appliedTokens.colors.primary).toBe("#ff0000");
    expect(result.current.tokens.colors.primary).toBe("#1a73e8");
  });

  it("resets dirty tokens", () => {
    const { result } = renderHook(() => useDesignTokens());
    act(() => {
      result.current.updateToken("colors", "primary", "#ff0000");
    });
    act(() => {
      result.current.resetTokens();
    });
    expect(result.current.isDirty).toBe(false);
    expect(result.current.appliedTokens.colors.primary).toBe("#1a73e8");
  });

  it("commits tokens", () => {
    const { result } = renderHook(() => useDesignTokens());
    const newTokens = { ...result.current.tokens, colors: { ...result.current.tokens.colors, primary: "#00ff00" } };
    act(() => {
      result.current.commitTokens(newTokens);
    });
    expect(result.current.tokens.colors.primary).toBe("#00ff00");
    expect(result.current.isDirty).toBe(false);
  });
});
