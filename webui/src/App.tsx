import { useMemo, useState } from "react";

import { AppFrame } from "@/ui/layout/AppFrame.tsx";
import { ConnectionBanner } from "@/ui/primitives";
import { useApp } from "@/hooks";
import { ConversationRoot } from "@/components/Conversation";
import { DetailsPanel } from "@/components/DetailsPanel";
import { SessionSidebar } from "@/components/SessionSidebar";
import { SettingsDialog } from "@/components/SettingsDialog";

export function App() {
  const connection = useApp((s) => s.connection);
  const booting = useApp((s) => s.booting);
  const current = useApp((s) => s.current);
  const sessionState = useApp((s) => (s.current ? s.byId.get(s.current) : undefined));
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);

  const hasActiveSession = Boolean(current && sessionState && !sessionState.summary.blank);
  const detailsActive = detailsOpen && hasActiveSession;

  const details = useMemo(
    () => <DetailsPanel onClose={() => setDetailsOpen(false)} />,
    [],
  );

  return (
    <>
      {connection === "reconnecting" ? (
        <div className="connection-banner-slot">
          <ConnectionBanner reconnecting label="连接已断开，正在重连…" />
        </div>
      ) : null}
      <AppFrame
        sidebar={(api) => (
          <SessionSidebar
            collapsed={api.collapsed}
            width={api.width}
            onToggle={api.toggle}
            onOpenSettings={() => setSettingsOpen(true)}
          />
        )}
        conversation={
          <ConversationRoot
            booting={booting}
            detailsOpen={detailsActive}
            onToggleDetails={
              hasActiveSession ? () => setDetailsOpen((v) => !v) : undefined
            }
          />
        }
        details={details}
        detailsActive={detailsActive}
        overlay={
          <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
        }
      />
    </>
  );
}
