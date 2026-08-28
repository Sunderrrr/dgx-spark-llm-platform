// Skeleton loading state for the home dashboard. The page is a client
// component that fetches /api/home on mount; without this, the content column
// renders blank until that aggregate round-trips (proxy -> Flask -> runner),
// which reads as "the page is slow". This fills the same shell with skeleton
// placeholders so the first paint shows structure instead of emptiness.
import { Layout, LayoutContent } from "@astryxdesign/core/Layout";
import { VStack, HStack, StackItem } from "@astryxdesign/core/Stack";
import { Card } from "@astryxdesign/core/Card";
import { Skeleton } from "@astryxdesign/core/Skeleton";

export default function Loading() {
  return (
    <Layout height="fill" content={
      <LayoutContent padding={6} isScrollable>
        <VStack gap={6}>
          <HStack hAlign="between" vAlign="center" wrap="wrap" gap={3}>
            <Skeleton width={240} height={28} />
            <Skeleton width={160} height={28} radius="rounded" />
          </HStack>

          <HStack wrap="wrap" gap={4}>
            {[0, 1, 2].map((i) => (
              <StackItem key={i}>
                <Card padding={6}>
                  <VStack gap={3}>
                    <Skeleton width={120} height={14} />
                    <Skeleton width={80} height={26} />
                  </VStack>
                </Card>
              </StackItem>
            ))}
          </HStack>

          <Card padding={6}>
            <VStack gap={3}>
              <Skeleton width="100%" height={180} radius={3} />
            </VStack>
          </Card>
        </VStack>
      </LayoutContent>
    } />
  );
}
