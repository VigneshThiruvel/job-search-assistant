import { useMutation, useQueryClient } from "@tanstack/react-query";

// A write followed by a refetch of everything it can change (lists, detail, counts).
export function useAction<A extends unknown[], R>(fn: (...args: A) => Promise<R>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (args: A) => fn(...args),
    onSuccess: () => client.invalidateQueries(),
  });
}
