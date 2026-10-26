import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "./app.css";
import { createTheme, MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { RunsProvider } from "./lib/runs";

const theme = createTheme({
  primaryColor: "indigo",
  defaultRadius: "md",
  fontFamily: "Inter, system-ui, -apple-system, 'Segoe UI', sans-serif",
});

// TanStack Query caches server data per key and refetches after mutations invalidate it,
// so components never hold their own copy of the queue.
const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: true, retry: 1 } },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="auto">
      <Notifications position="bottom-right" />
      <QueryClientProvider client={queryClient}>
        <RunsProvider>
          <App />
        </RunsProvider>
      </QueryClientProvider>
    </MantineProvider>
  </StrictMode>,
);
