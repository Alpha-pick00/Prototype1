import { useState, useEffect } from 'react';
import { HashRouter as Router, Routes, Route, useLocation } from 'react-router-dom';
import { motion, AnimatePresence } from 'motion/react';
import { Hero } from './components/Hero';
import { About } from './components/About';
import { Projects } from './components/Projects';
import { Services } from './components/Services';
import { HowWeCurate } from './components/HowWeCurate';
import { Footer } from './components/Footer';
import { Navbar } from './components/Navbar';
import { Sidebar } from './components/Sidebar';
import { Work } from './components/Work';
import { AuthProvider } from './context/AuthContext';
import { SearchProvider, useSearch } from './context/SearchContext';
import { SidebarProvider, useSidebar } from './context/SidebarContext';

// Preloader Component
const Preloader = () => (
  <motion.div
    initial={{ opacity: 0 }}
    animate={{ opacity: 1 }}
    exit={{ opacity: 0, transition: { duration: 0.8, ease: "easeInOut" } }}
    className="fixed inset-0 z-[999] bg-white flex items-center justify-center text-black"
  >
    <motion.div
      initial={{ opacity: 0, scale: 0.8, filter: "blur(10px)" }}
      animate={{ opacity: 1, scale: 1, filter: "blur(0px)" }}
      transition={{ duration: 1, ease: [0.22, 1, 0.36, 1] }}
      className="flex flex-col items-center gap-4"
    >
      <h1 className="text-4xl md:text-6xl font-bold tracking-tighter" style={{ color: 'rgb(64,117,38)' }}>
        αlpha Pick
      </h1>
      <motion.div 
        initial={{ width: 0 }}
        animate={{ width: "100%" }}
        transition={{ delay: 0.5, duration: 1.5, ease: "easeInOut" }}
        className="h-px bg-black/20 w-32"
      />
    </motion.div>
  </motion.div>
);

// Enhanced ScrollToTop to handle both routes and hash anchors
const ScrollToTop = () => {
  const { pathname, hash } = useLocation();

  useEffect(() => {
    if (hash) {
      setTimeout(() => {
        const element = document.querySelector(hash);
        if (element) {
          element.scrollIntoView({ behavior: 'smooth' });
        }
      }, 100);
    } else {
      window.scrollTo(0, 0);
    }
  }, [pathname, hash]);

  return null;
};

const HomePage = () => {
  // 대화가 시작되면(사용자 요청, 2026-08-15: "채팅이 메인이되게 해줘") 랜딩
  // 페이지의 나머지 섹션(About/Projects/Services/HowWeCurate/Footer)은 아예
  // 렌더하지 않는다 - Hero가 h-screen이어도 이 섹션들이 DOM에 남아있으면 그
  // 아래로 스크롤이 이어져서 다른 LLM 챗 앱들과 달리 채팅이 전체 화면을 차지하지
  // 않았다. 대화 시작 전에는 지금처럼 그대로 내려서 보인다.
  const { turns } = useSearch();
  const hasConversation = turns.length > 0;

  return (
    <>
      <Hero />
      {!hasConversation && (
        <>
          <About />
          <Projects />
          <Services />
          <HowWeCurate />
          <Footer />
        </>
      )}
    </>
  );
};

// 사이드바 padding-left만 반응하는 잎(leaf) 컴포넌트로 분리했다(2026-08-28,
// 모바일 사이드바 토글 렉 리포트). 원래 AppShell이 직접 useSidebar()를 읽어
// isOpen을 <Routes> 바로 위 래퍼에 넘겼는데, 그러면 isOpen이 바뀔 때마다
// AppShell 자체가 리렌더되면서 그 아래 <Routes>(대화 중이면 Hero.tsx의 채팅
// 스레드 전체 + framer-motion 애니메이션까지)가 통째로 다시 렌더링됐다 -
// 데스크톱은 CPU 여유로 안 느껴지지만 모바일에서는 그대로 렉으로 드러남.
// children을 prop으로 받아 컴포지션으로 분리하면, AppShell은 더 이상
// useSidebar()를 구독하지 않으니 사이드바 토글에 전혀 반응하지 않고(children
// 엘리먼트 참조도 그대로 유지돼 재사용됨), isOpen 변화는 이 작은 래퍼만
// 리렌더시킨다.
const SidebarPaddedContent = ({ children }: { children: React.ReactNode }) => {
  const { isOpen } = useSidebar();
  return (
    <div
      className={`transition-[padding-left] duration-300 ease-out ${
        isOpen ? 'md:pl-[280px]' : 'md:pl-[68px]'
      }`}
    >
      {children}
    </div>
  );
};

const AppShell = () => {
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Intro animation duration
    const timer = setTimeout(() => {
      setLoading(false);
    }, 2000);
    return () => clearTimeout(timer);
  }, []);

  return (
    <Router>
      <ScrollToTop />

      <AnimatePresence mode="wait">
        {loading && <Preloader key="preloader" />}
      </AnimatePresence>

      {!loading && (
        <div className="bg-white min-h-screen text-neutral-950 selection:bg-black/20">
          {/* Navbar/Sidebar는 항상 fixed라 화면 기준으로 고정돼야 하는 전역 UI다 - 페이지
              본문과 같은 padding 박스 안에 두면 안 된다(2026-08-12: Work/About/Services/
              Contact가 사이드바 열림에 반응해 밀리다 잘리는 문제였다). 그래서 이 둘은
              padding이 걸리는 아래 div 밖의 형제로 뺐다 - 사이드바가 밀어내야 하는 건
              실제 페이지 컨텐츠(Routes)뿐이다. */}
          <Navbar />
          <Sidebar />
          {/* 사이드바 패널이 열려있으면 본문을 그만큼 오른쪽으로 밀어낸다(모달처럼 덮어서
              어둡게 가리는 대신, 옆에 도킹된 패널처럼) - 2026-08-12 요청. 두 값 다 리터럴
              클래스 문자열로 써둬야 Tailwind가 빌드 시점에 인식해 CSS를 만들어낸다.
              isOpen 구독은 SidebarPaddedContent 안으로 옮겼다 - 위 주석 참고. */}
          <SidebarPaddedContent>
            <Routes>
              <Route path="/" element={<HomePage />} />
              <Route path="/work" element={<Work />} />
            </Routes>
          </SidebarPaddedContent>
        </div>
      )}
    </Router>
  );
};

function App() {
  return (
    <AuthProvider>
      <SearchProvider>
        <SidebarProvider>
          <AppShell />
        </SidebarProvider>
      </SearchProvider>
    </AuthProvider>
  );
}

export default App;
