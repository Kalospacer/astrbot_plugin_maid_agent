import { useState } from "react";

import { AppFrame } from "@/ui/layout/AppFrame.tsx";
import { ConnectionBanner } from "@/ui/primitives";
import { useApp } from "@/hooks";
import { ConversationRoot } from "@/components/Conversation";
import { SessionSidebar } from "@/components/SessionSidebar";
import { SettingsDialog } from "@/components/SettingsDialog";

export function App() {
  const connection = useApp((s) => s.connection);
  const booting = useApp((s) => s.booting);
  const [settingsOpen, setSettingsOpen] = useState(false);

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
        conversation={<ConversationRoot booting={booting} />}
        overlay={
          <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
        }
      />
    </>
  );
}
