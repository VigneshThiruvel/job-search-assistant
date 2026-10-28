import { AppShell, Badge, Burger, Group, NavLink, ScrollArea, Text, Title } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { IconBriefcase, IconChartDots3, IconChecklist, IconHistory, IconInbox } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Code } from "@mantine/core";
import { Component, useEffect, useState, type ReactNode } from "react";
import { api } from "./api/client";
import HealthBar from "./components/HealthBar";
import RunDrawer from "./components/RunDrawer";
import ActivityPage from "./pages/ActivityPage";
import ApplicationsPage from "./pages/ApplicationsPage";
import InboxPage from "./pages/InboxPage";
import SkillsPage from "./pages/SkillsPage";
import TodayPage from "./pages/TodayPage";

const PAGES = {
  today: { label: "Today", icon: IconChecklist },
  inbox: { label: "Inbox", icon: IconInbox },
  applications: { label: "Applications", icon: IconBriefcase },
  skills: { label: "Skills", icon: IconChartDots3 },
  activity: { label: "Activity", icon: IconHistory },
} as const;
export type Page = keyof typeof PAGES;

function pageFromHash(): Page {
  const hash = window.location.hash.slice(1).split("/")[0];
  return hash in PAGES ? (hash as Page) : "today";
}

export function go(page: Page, sub?: string | number) {
  window.location.hash = sub === undefined ? page : `${page}/${sub}`;
}

// A render error shows a message on that page instead of blanking the whole app.
class PageErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <Alert color="red" title="This page hit an error">
        <Code block mb="sm">{this.state.error.message}</Code>
        Most often the API server runs older code than this page: stop it (Ctrl+C) and start it
        again with <Code>uv run python -m job_search_assistant.api</Code>, then reload.
      </Alert>
    );
  }
}

export default function App() {
  const [page, setPage] = useState<Page>(pageFromHash);
  const [opened, { toggle, close }] = useDisclosure();
  const status = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 15000 });

  useEffect(() => {
    const onHash = () => setPage(pageFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const s = status.data;
  const counts: Partial<Record<Page, number>> = {
    today: s?.queued_today,
    inbox: s?.inbox_open,
    applications: s ? s.selected + s.ready : undefined,
  };
  const views: Record<Page, ReactNode> = {
    today: <TodayPage />,
    inbox: <InboxPage />,
    applications: <ApplicationsPage />,
    skills: <SkillsPage />,
    activity: <ActivityPage />,
  };

  return (
    <AppShell header={{ height: 56 }} navbar={{ width: 220, breakpoint: "sm", collapsed: { mobile: !opened } }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between" wrap="nowrap">
          <Group gap="sm" wrap="nowrap">
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" />
            <Title order={4}>Job search</Title>
          </Group>
          <HealthBar />
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="xs">
        <ScrollArea>
          {(Object.keys(PAGES) as Page[]).map((p) => {
            const { label, icon: Icon } = PAGES[p];
            return (
              <NavLink
                key={p}
                href={`#${p}`}
                label={label}
                active={p === page}
                onClick={close}
                leftSection={<Icon size={18} stroke={1.6} />}
                rightSection={counts[p] ? <Badge size="sm" variant="light" circle>{counts[p]}</Badge> : null}
              />
            );
          })}
        </ScrollArea>
        <Text size="xs" c="dimmed" mt="auto" p="xs">
          Drafts only: you review and submit every application yourself.
        </Text>
      </AppShell.Navbar>
      <AppShell.Main>
        <PageErrorBoundary key={page}>{views[page]}</PageErrorBoundary>
      </AppShell.Main>
      <RunDrawer />
    </AppShell>
  );
}
