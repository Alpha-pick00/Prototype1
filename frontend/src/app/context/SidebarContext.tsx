import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';

// 사이드바 열림 상태를 Sidebar 바깥(App의 레이아웃 padding, Hero의 고정 로고 위치)에서도
// 알아야 해서(2026-08-12: "옆으로 반응형으로 가게") 이 상태를 Sidebar 로컬이 아니라
// context로 끌어올렸다 - App이 이걸 보고 본문 padding을 넓혀 사이드바를 "덮는" 모달이
// 아니라 "밀어내는" 도크로 만든다.
interface SidebarContextValue {
  isOpen: boolean;
  open: () => void;
  close: () => void;
  toggle: () => void;
}

const SidebarContext = createContext<SidebarContextValue | null>(null);

export const SidebarProvider = ({ children }: { children: React.ReactNode }) => {
  const [isOpen, setIsOpen] = useState(false);

  // open/close/toggle을 useCallback으로 고정하고 value 전체를 useMemo로 묶는다
  // (2026-08-28, 모바일 사이드바 토글 렉 리포트) - 이게 없으면 매 렌더마다 새
  // 객체+새 함수 참조가 만들어져, isOpen이 실제로 안 바뀐 리렌더에서도 이
  // context를 구독하는 모든 컴포넌트가 다시 렌더링된다. 주된 렉 원인은
  // App.tsx 쪽(AppShell이 isOpen을 직접 구독해 <Routes> 전체를 리렌더시키던
  // 것 - SidebarPaddedContent로 분리해 해결)이었지만, 이 메모이제이션도 같이
  // 해둬야 다른 소비자가 늘어나도 같은 문제가 재발하지 않는다.
  const open = useCallback(() => setIsOpen(true), []);
  const close = useCallback(() => setIsOpen(false), []);
  const toggle = useCallback(() => setIsOpen((prev) => !prev), []);
  const value = useMemo(() => ({ isOpen, open, close, toggle }), [isOpen, open, close, toggle]);

  return <SidebarContext.Provider value={value}>{children}</SidebarContext.Provider>;
};

export const useSidebar = () => {
  const ctx = useContext(SidebarContext);
  if (!ctx) throw new Error('useSidebar는 SidebarProvider 내부에서만 사용할 수 있습니다.');
  return ctx;
};
