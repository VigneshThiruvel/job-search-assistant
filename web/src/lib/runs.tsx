// Runs (fetch, embed, agents, Neo4j) are started from several pages and watched in one
// drawer. This context holds which run the drawer shows; TanStack Query polls its log.
import { notifications } from "@mantine/notifications";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useState, type ReactNode } from "react";
import { api, type RunKind } from "../api/client";

type RunsContext = { watching: number | null; watch: (id: number | null) => void };
const Ctx = createContext<RunsContext>({ watching: null, watch: () => {} });

export function RunsProvider({ children }: { children: ReactNode }) {
  const [watching, watch] = useState<number | null>(null);
  return <Ctx.Provider value={{ watching, watch }}>{children}</Ctx.Provider>;
}

export const useRuns = () => useContext(Ctx);

export function useStartRun() {
  const { watch } = useRuns();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: { kind: RunKind; k?: number; maxExtract?: number }) =>
      api.startRun(args.kind, args.k, args.maxExtract),
    onSuccess: (run) => {
      watch(run.id);
      client.invalidateQueries({ queryKey: ["health"] });
      client.invalidateQueries({ queryKey: ["runs"] });
    },
    onError: (e: Error) => notifications.show({ color: "red", title: "Could not start", message: e.message }),
  });
}
