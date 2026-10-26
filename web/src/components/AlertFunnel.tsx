import { Anchor, Badge, Button, Card, Collapse, Group, Stack, Table, Text, Title } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { useQuery } from "@tanstack/react-query";
import { go } from "../App";
import { api, type AlertJob } from "../api/client";

// Why each recent LinkedIn/XING job is (not) in the queue. Reasons come from the same
// filter rules the ranking uses (search.explain), not from the agent's report.
const STAGES: Record<AlertJob["stage"], { label: string; color: string; hint: string }> = {
  queued: { label: "queued", color: "teal", hint: "in your review queue" },
  eligible: { label: "passes filters", color: "blue", hint: "the next daily agent run ranks it" },
  not_extracted: { label: "not extracted yet", color: "yellow", hint: "the next daily agent run extracts it" },
  needs_description: { label: "needs description", color: "orange", hint: "paste it in the Inbox" },
  filtered: { label: "filtered out", color: "red", hint: "fails one of your rules" },
  expired: { label: "expired", color: "gray", hint: "you marked it closed" },
};

export default function AlertFunnel() {
  const funnel = useQuery({ queryKey: ["alerts"], queryFn: api.alerts });
  const [open, { toggle }] = useDisclosure(false);
  const f = funnel.data;
  if (!f) return null;
  const count = (stage: AlertJob["stage"]) => f.jobs.filter((j) => j.stage === stage).length;
  const queued = count("queued");
  const filtered = f.jobs.filter((j) => j.stage === "filtered");
  const topReason = mostCommon(filtered.flatMap((j) => j.reasons));
  return (
    <Card withBorder padding="sm">
      <Stack gap={6}>
        <Group justify="space-between" wrap="nowrap">
          <div>
            <Title order={6}>From your alert emails (last 2 days)</Title>
            <Text size="sm">
              {f.jobs.length === 0
                ? "No relevant LinkedIn/XING jobs in the last two days."
                : `${f.jobs.length} relevant job(s), ${queued} in your queue.` +
                  (filtered.length ? ` ${filtered.length} filtered out${topReason ? `, mostly: ${topReason}` : ""}.` : "")}
            </Text>
          </div>
          {f.jobs.length > 0 && <Button size="xs" variant="subtle" onClick={toggle}>{open ? "Hide" : "Why?"}</Button>}
        </Group>
        <Group gap={6}>
          {(Object.keys(STAGES) as AlertJob["stage"][]).map((s) =>
            count(s) ? <Badge key={s} color={STAGES[s].color} variant="light">{count(s)} {STAGES[s].label}</Badge> : null,
          )}
          {f.other_titles > 0 && (
            <Badge color="gray" variant="outline" title="Not your roles by title, senior or student roles">
              {f.other_titles} other titles skipped
            </Badge>
          )}
        </Group>
        <Collapse in={open}>
          <Table mt="xs" verticalSpacing={4} fz="sm">
            <Table.Tbody>
              {f.jobs.map((j) => (
                <Table.Tr key={j.job_id}>
                  <Table.Td>
                    <Anchor href={j.url} target="_blank" size="sm">{j.title}</Anchor>
                    <Text size="xs" c="dimmed">{j.company} · {j.source}</Text>
                  </Table.Td>
                  <Table.Td>
                    <Badge size="sm" color={STAGES[j.stage].color} variant="light">{STAGES[j.stage].label}</Badge>
                  </Table.Td>
                  <Table.Td>
                    <Text size="xs">
                      {j.reasons.length ? j.reasons.join(", ") : STAGES[j.stage].hint}
                    </Text>
                    {j.stage === "needs_description" && <Anchor size="xs" onClick={() => go("inbox")}>Open inbox</Anchor>}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Collapse>
      </Stack>
    </Card>
  );
}

function mostCommon(items: string[]): string | null {
  const counts = new Map<string, number>();
  for (const i of items) counts.set(i, (counts.get(i) ?? 0) + 1);
  let best: string | null = null;
  for (const [k, v] of counts) if (best === null || v > counts.get(best)!) best = k;
  return best;
}
