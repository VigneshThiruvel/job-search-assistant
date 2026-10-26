import { Alert, Badge, Button, Card, Group, SegmentedControl, Stack, Table, Text, TextInput, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type InsightKind } from "../api/client";
import { useAction } from "../lib/useAction";

const KINDS: { value: InsightKind; label: string; hint: string }[] = [
  { value: "gaps", label: "Gaps", hint: "Skills you lack, weighted by how many jobs need them (required 1, nice 0.5; halved if you have a broader skill)." },
  { value: "unlock", label: "Unlocks", hint: "Skills that are the only missing requirement of a job: learn one, qualify fully." },
  { value: "coverage", label: "Coverage", hint: "How much of each job's required skills you cover." },
  { value: "roles", label: "Roles", hint: "Most-required skills per role family." },
  { value: "together", label: "Together", hint: "Skills listed alongside a given skill." },
];

function cell(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "number") return String(Math.round(value * 100) / 100);
  return value === null || value === undefined ? "–" : String(value);
}

export default function SkillsPage() {
  const [kind, setKind] = useState<InsightKind>("gaps");
  const [scope, setScope] = useState<"target" | "eligible">("target");
  const [skill, setSkill] = useState("Apache Kafka");
  const insight = useQuery({
    queryKey: ["insights", kind, scope, kind === "together" ? skill : ""],
    queryFn: () => api.insights(kind, scope, 20, kind === "together" ? skill : undefined),
    enabled: kind !== "together" || skill.length > 1,
  });
  const rows = insight.data?.rows ?? [];
  const columns = rows.length ? Object.keys(rows[0]) : [];
  return (
    <Stack>
      <Title order={4}>Skills</Title>
      <Card withBorder>
        <Stack gap="sm">
          <Group justify="space-between">
            <SegmentedControl value={kind} onChange={(v) => setKind(v as InsightKind)} data={KINDS} />
            <SegmentedControl value={scope} onChange={(v) => setScope(v as "target" | "eligible")}
              data={[{ value: "target", label: "Whole market" }, { value: "eligible", label: "Jobs I can apply to" }]} />
          </Group>
          <Text size="sm" c="dimmed">{KINDS.find((x) => x.value === kind)?.hint}</Text>
          {kind === "together" && <TextInput label="Skill (canonical name)" value={skill} onChange={(e) => setSkill(e.currentTarget.value)} w={260} />}
          {insight.data && !insight.data.available && <Alert color="orange">Start Neo4j (top bar) to query the knowledge graph.</Alert>}
          {rows.length > 0 && (
            <Table.ScrollContainer minWidth={500}>
              <Table striped highlightOnHover>
                <Table.Thead>
                  <Table.Tr>{columns.map((c) => <Table.Th key={c}>{c.replace(/_/g, " ")}</Table.Th>)}</Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {rows.map((r, i) => (
                    <Table.Tr key={i}>{columns.map((c) => <Table.Td key={c}>{cell(r[c])}</Table.Td>)}</Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          )}
        </Stack>
      </Card>
      <MappingReview />
    </Stack>
  );
}

function MappingReview() {
  const review = useQuery({ queryKey: ["skill-review"], queryFn: api.skillReview });
  const exportYaml = useAction(api.exportSkills, "Exported to taxonomy/skills.yaml: review the git diff");
  const r = review.data;
  if (!r) return null;
  return (
    <Card withBorder>
      <Group justify="space-between" mb="sm">
        <div>
          <Title order={5}>New skill mappings</Title>
          <Text size="sm" c="dimmed">Mappings the agent added since your last export. Export, then review the diff of taxonomy/skills.yaml.</Text>
        </div>
        <Button disabled={r.new.length === 0 || r.unimported.length > 0} loading={exportYaml.isPending} onClick={() => exportYaml.mutate([])}>
          Export for review
        </Button>
      </Group>
      {r.unimported.length > 0 && (
        <Alert color="orange" mb="sm" title="taxonomy/skills.yaml has edits that are not imported">
          Run <code>uv run python -m job_search_assistant.skills import</code> first, or exporting would overwrite them.
          {r.unimported.slice(0, 5).map((u) => <Text key={u} size="xs">{u}</Text>)}
        </Alert>
      )}
      {r.new.length === 0 ? (
        <Text size="sm" c="dimmed">Nothing new: the YAML matches the database.</Text>
      ) : (
        <Table>
          <Table.Thead><Table.Tr><Table.Th>As written</Table.Th><Table.Th>Mapped to</Table.Th><Table.Th>Category</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>
            {r.new.map((m) => (
              <Table.Tr key={m.raw}>
                <Table.Td>{m.raw}</Table.Td>
                <Table.Td>{m.skill ?? <Badge color="gray" variant="light">dropped</Badge>}</Table.Td>
                <Table.Td>{m.category ?? "–"}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Card>
  );
}
