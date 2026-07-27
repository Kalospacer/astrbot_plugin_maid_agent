/**
 * 50 个二次元女角色名（真实动画作品），按 agent_id 稳定映射，
 * 避免在 UI 上直接暴露哈希 ID。从 1.4.1 的 pages/console/app.js 移植。
 */

const ANIME_ALIASES = [
  "Ganyu", "Mafuyu", "Sakiko", "Hu Tao", "Raiden Shogun", "Yae Miko", "Nahida",
  "Furina", "Navia", "Arlecchino", "Shenhe", "Yelan", "Nilou", "Kaveh",
  "Alhaitham", "Eula", "Ayaka", "Yoimiya", "Itto", "Sara",
  "Miko", "Collei", "Lumine", "Aether", "Paimon",
  "Makima", "Power", "Kobeni", "Himeno", "Reze",
  "Yor", "Anya", "Becky", "Fiona", "Sylvia",
  "Rika", "Touko", "Sayaka", "Kyouko", "Homura",
  "Madoka", "Mami", "Nagisa", "Hitomi", "Kyoko",
  "Reina", "Kumiko", "Asuka", "Haruka", "Aoi",
];

/** FNV-1a 哈希 → 稳定落到别名表，同一 agent_id 永远同名。 */
export function aliasForAgentId(agentId) {
  const id = String(agentId || "");
  if (!id) return "Unknown";
  let hash = 2166136261;
  for (let i = 0; i < id.length; i += 1) {
    hash ^= id.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  const idx = Math.abs(hash) % ANIME_ALIASES.length;
  return ANIME_ALIASES[idx];
}

/** 会话标题：后端生成的 title 优先，回退到「别名 · agent_name」。 */
export function displayAgentTitle(agent) {
  if (!agent) return "";
  return agent.title || `${aliasForAgentId(agent.agent_id)} · ${agent.agent_name}`;
}
