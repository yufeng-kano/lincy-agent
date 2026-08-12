"""Kernel migrations registry."""

from .m0001_initial import M0001Initial
from .m0002_agents_structure import M0002AgentsStructure
from .m0003_prompt_v3 import M0003PromptV3
from .m0004_shutdown_v2 import M0004ShutdownV2
from .m0005_reviewer_prompts import M0005ReviewerPrompts
from .m0006_reviewer_agents import M0006ReviewerAgents
from .m0007_post_reviewer_prompt_tuning import M0007PostReviewerPromptTuning
from .m0008_post_reviewer_structured_actions import (
    M0008PostReviewerStructuredActions,
)
from .m0009_shutdown_reviewer_prompt import M0009ShutdownReviewerPrompt
from .m0010_reviewer_parse_retry_prompts import M0010ReviewerParseRetryPrompts
from .m0011_system_prompt_formatting import M0011SystemPromptFormatting
from .m0012_turn_persistence_prompt_tuning import (
    M0012TurnPersistencePromptTuning,
)
from .m0013_memory_writer_pipeline import M0013MemoryWriterPipeline
from .m0014_recent_context_priority import M0014RecentContextPriority
from .m0015_post_review_packet_prompt import M0015PostReviewPacketPrompt
from .m0016_replace_block_prompt_update import M0016ReplaceBlockPromptUpdate
from .m0017_inner_state_discipline import M0017InnerStateDiscipline
from .m0018_trivial_turn_exemption_widen import M0018TrivialTurnExemptionWiden
from .m0019_review_packet_violations import M0019ReviewPacketViolations
from .m0020_empty_reply_violation import M0020EmptyReplyViolation
from .m0021_memory_searcher import M0021MemorySearcher
from .m0022_post_reviewer_zh_tw import M0022PostReviewerZhTw
from .m0023_brain_prompt_zh_tw import M0023BrainPromptZhTw
from .m0024_reviewer_enforcement import M0024ReviewerEnforcement
from .m0025_remove_editor_llm import M0025RemoveEditorLlm
from .m0026_label_requires_persistence import M0026LabelRequiresPersistence
from .m0027_memory_search_no_index_results import M0027MemorySearchNoIndexResults
from .m0028_memory_edit_v2_intent_pipeline import M0028MemoryEditV2IntentPipeline
from .m0029_post_reviewer_label_stability import M0029PostReviewerLabelStability
from .m0030_strict_target_anomaly_signals import M0030StrictTargetAnomalySignals
from .m0031_memory_search_two_stage_configurable_limits import (
    M0031MemorySearchTwoStageConfigurableLimits,
)
from .m0032_delete_file_index_sync import M0032DeleteFileIndexSync
from .m0033_memory_search_zh_tw import M0033MemorySearchZhTw
from .m0034_memory_edit_ordering_rule import M0034MemoryEditOrderingRule
from .m0035_scope_boundary_prompts import M0035ScopeBoundaryPrompts
from .m0036_memory_short_term_move import M0036MemoryShortTermMove
from .m0037_context_window_boot import M0037ContextWindowBoot
from .m0038_skills_first_shell import M0038SkillsFirstShell
from .m0039_long_term_memory import M0039LongTermMemory
from .m0040_persona_trigger import M0040PersonaTrigger
from .m0041_memory_edit_overwrite import M0041MemoryEditOverwrite
from .m0042_vision_agent import M0042VisionAgent
from .m0043_people_folder import M0043PeopleFolder
from .m0044_people_search_trigger import M0044PeopleSearchTrigger
from .m0045_multi_intent_preference import M0045MultiIntentPreference
from .m0046_gui_agents import M0046GuiAgents
from .m0047_session_reorganize import M0047SessionReorganize
from .m0048_gui_report_problem import M0048GuiReportProblem
from .m0049_gui_resume_state import M0049GuiResumeState
from .m0050_brain_screenshot import M0050BrainScreenshot
from .m0051_gui_obstacle_awareness import M0051GuiObstacleAwareness
from .m0052_gui_unautomatable_escalation import M0052GuiUnautomatableEscalation
from .m0053_gui_scroll_awareness import M0053GuiScrollAwareness
from .m0054_gui_human_browsing import M0054GuiHumanBrowsing
from .m0055_gui_force_tool_call import M0055GuiForceToolCall
from .m0056_brain_tool_immediate import M0056BrainToolImmediate
from .m0057_gui_right_click_maximize import M0057GuiRightClickMaximize
from .m0058_gui_scroll_keys import M0058GuiScrollKeys
from .m0059_gui_prompt_rewrite import M0059GuiPromptRewrite
from .m0060_brain_prompt_v2 import M0060BrainPromptV2
from .m0061_gui_task_guidance import M0061GuiTaskGuidance
from .m0062_people_profile_split import M0062PeopleProfileSplit
from .m0063_gui_scan_layout import M0063GuiScanLayout
from .m0064_read_image_by_subagent import M0064ReadImageBySubagent
from .m0065_gui_scroll_drag import M0065GuiScrollDrag
from .m0066_progress_reviewer import M0066ProgressReviewer
from .m0067_completion_reviewer_prompts import M0067CompletionReviewerPrompts
from .m0068_gui_scroll_layout_prompts import M0068GuiScrollLayoutPrompts
from .m0069_agent_os_dir_awareness import M0069AgentOsDirAwareness
from .m0070_memory_sync_prompt import M0070MemorySyncPrompt
from .m0071_remove_reviewer_shutdown import M0071RemoveReviewerShutdown
from .m0072_sender_aware_messages import M0072SenderAwareMessages
from .m0073_conversational_default import M0073ConversationalDefault
from .m0074_gmail_adapter import M0074GmailAdapter
from .m0075_send_message import M0075SendMessage
from .m0076_send_message_strict import M0076SendMessageStrict
from .m0077_gui_app_prompt import M0077GuiAppPrompt
from .m0078_send_message_attachments import M0078SendMessageAttachments
from .m0079_heartbeat import M0079Heartbeat
from .m0080_thread_registry import M0080ThreadRegistry
from .m0081_thread_prompt_refine import M0081ThreadPromptRefine
from .m0082_schedule_followup import M0082ScheduleFollowup
from .m0084_boot_context_split import M0084BootContextSplit
from .m0085_merge_recent_memory import M0085MergeRecentMemory
from .m0086_bm25_memory_search import M0086Bm25MemorySearch
from .m0087_memory_edit_index_warnings import M0087MemoryEditIndexWarnings
from .m0088_memory_maintenance_stream_json import M0088MemoryMaintenanceStreamJson
from .m0089_screenshot_by_subagent import M0089ScreenshotBySubagent
from .m0090_remove_kernel_timezone import M0090RemoveKernelTimezone
from .m0091_discord_adapter import M0091DiscordAdapter
from .m0092_discord_prompt_style import M0092DiscordPromptStyle
from .m0093_discord_prompt_single_line import M0093DiscordPromptSingleLine
from .m0094_discord_prompt_examples import M0094DiscordPromptExamples
from .m0096_brain_prompt_edge_cases import M0096BrainPromptEdgeCases
from .m0097_provider_thinking_mapping_fix import M0097ProviderThinkingMappingFix
from .m0098_brain_relationship_correctness import M0098BrainRelationshipCorrectness
from .m0099_brain_cooldown_natural_care import M0099BrainCooldownNaturalCare
from .m0100_brain_state_signal_framing import M0100BrainStateSignalFraming
from .m0101_brain_prompt_restructure import M0101BrainPromptRestructure
from .m0102_brain_prompt_iron_rules_refresh import M0102BrainPromptIronRulesRefresh
from .m0103_copilot_reasoning_visibility import M0103CopilotReasoningVisibility
from .m0104_brain_prompt_think_then_act import M0104BrainPromptThinkThenAct
from .m0105_impulse_system import M0105ImpulseSystem
from .m0106_vision_no_hallucination import M0106VisionNoHallucination
from .m0107_gui_scroll_position import M0107GuiScrollPosition
from .m0108_gui_task_background import M0108GuiTaskBackground
from .m0109_builtin_skills import M0109BuiltinSkills
from .m0110_brain_send_message_segments import M0110BrainSendMessageSegments
from .m0111_send_message_single_body import M0111SendMessageSingleBody
from .m0112_send_message_parallel import M0112SendMessageParallel
from .m0113_discord_markdown_prompt import M0113DiscordMarkdownPrompt
from .m0114_discord_builtin_skill import M0114DiscordBuiltinSkill
from .m0115_discord_presentation_strategy import M0115DiscordPresentationStrategy
from .m0116_discord_natural_lists import M0116DiscordNaturalLists
from .m0117_discord_message_economy import M0117DiscordMessageEconomy
from .m0118_skill_prerequisite_metadata import M0118SkillPrerequisiteMetadata
from .m0119_discord_dm_single_line import M0119DiscordDmSingleLine
from .m0120_shell_noninteractive import M0120ShellNonInteractive
from .m0121_shell_task import M0121ShellTask
from .m0122_web_search import M0122WebSearch
from .m0123_shell_task_handoff import M0123ShellTaskHandoff
from .m0124_discord_emoji_newline import M0124DiscordEmojiNewline
from .m0125_brain_prompt_fragments import M0125BrainPromptFragments
from .m0126_remove_memory_searcher import M0126RemoveMemorySearcher
from .m0127_web_fetch import M0127WebFetch
from .m0128_gui_loading_scroll_prompts import M0128GuiLoadingScrollPrompts
from .m0129_memory_editor_long_term_routing import (
    M0129MemoryEditorLongTermRouting,
)
from .m0130_memory_editor_long_term_structure_guard import (
    M0130MemoryEditorLongTermStructureGuard,
)
from .m0131_long_term_lists import M0131LongTermLists
from .m0132_long_term_core_values import M0132LongTermCoreValues
from .m0133_agent_task_note import M0133AgentTaskNote
from .m0134_discord_attachment_context import M0134DiscordAttachmentContext
from .m0135_skill_create_guide import M0135SkillCreateGuide
from .m0136_skill_md_format import M0136SkillMdFormat
from .m0137_skill_installer_repo_at_skill import M0137SkillInstallerRepoAtSkill
from .m0138_personal_skills_root import M0138PersonalSkillsRoot
from .m0139_skill_checker_agent import M0139SkillCheckerAgent
from .m0140_memory_maintenance_builtin import M0140MemoryMaintenanceBuiltin
from .m0141_end_of_turn_tool import M0141EndOfTurnTool
from .m0142_worker_subagent import M0142WorkerSubagent
from .m0143_brain_worker_tool_docs import M0143BrainWorkerToolDocs
from .m0144_web_fetch_prompt_docs import M0144WebFetchPromptDocs
from .m0145_worker_env_rules import M0145WorkerEnvRules
from .m0146_apple_apps_context import M0146AppleAppsContext
from .m0147_icloud_sync_prompt_fragment import M0147ICloudSyncPromptFragment
from .m0148_remove_apple_apps_auto_sync import M0148RemoveAppleAppsAutoSync
from .m0149_apple_notes_cache import M0149AppleNotesCache
from .m0150_notes_template_markdown import M0150NotesTemplateMarkdown
from .m0151_notes_template_title_semantics import M0151NotesTemplateTitleSemantics
from .m0152_notes_title_body_rules import M0152NotesTitleBodyRules
from .m0153_self_improvement_prompt import M0153SelfImprovementPrompt
from .m0154_mail_tool_prompt import M0154MailToolPrompt
from .m0155_state_commit_tool_budget import M0155StateCommitToolBudget
from .m0156_schedule_action_batch import M0156ScheduleActionBatch
from .m0157_temp_memory_append_only import M0157TempMemoryAppendOnly
from .m0158_heartbeat_reliability_prompt import M0158HeartbeatReliabilityPrompt
from .m0159_reminders_due_timezone_fix import M0159RemindersDueTimezoneFix
from .m0160_ax_first_gui import M0160AxFirstGui
from .m0161_backup_scope_kernel_only import M0161BackupScopeKernelOnly
from .m0162_discord_inbound_structure import M0162DiscordInboundStructure
from .m0163_brain_shell_delegation import M0163BrainShellDelegation
from .m0164_memory_maintenance_worker_native import M0164MemoryMaintenanceWorkerNative
from .m0165_worker_async_dispatch import M0165WorkerAsyncDispatch
from .m0166_worker_memory_file_access import M0166WorkerMemoryFileAccess
from .m0167_temp_memory_delta_write import M0167TempMemoryDeltaWrite
from .m0168_remove_gui_display_limits import M0168RemoveGuiDisplayLimits
from .m0169_memory_curation_warnings import M0169MemoryCurationWarnings
from .m0170_memory_curation import M0170MemoryCuration
from .m0171_remove_memory_curator_agent import M0171RemoveMemoryCuratorAgent
from .m0172_compactor_agent import M0172CompactorAgent

ALL_MIGRATIONS = [
    M0001Initial(),
    M0002AgentsStructure(),
    M0003PromptV3(),
    M0004ShutdownV2(),
    M0005ReviewerPrompts(),
    M0006ReviewerAgents(),
    M0007PostReviewerPromptTuning(),
    M0008PostReviewerStructuredActions(),
    M0009ShutdownReviewerPrompt(),
    M0010ReviewerParseRetryPrompts(),
    M0011SystemPromptFormatting(),
    M0012TurnPersistencePromptTuning(),
    M0013MemoryWriterPipeline(),
    M0014RecentContextPriority(),
    M0015PostReviewPacketPrompt(),
    M0016ReplaceBlockPromptUpdate(),
    M0017InnerStateDiscipline(),
    M0018TrivialTurnExemptionWiden(),
    M0019ReviewPacketViolations(),
    M0020EmptyReplyViolation(),
    M0021MemorySearcher(),
    M0022PostReviewerZhTw(),
    M0023BrainPromptZhTw(),
    M0024ReviewerEnforcement(),
    M0025RemoveEditorLlm(),
    M0026LabelRequiresPersistence(),
    M0027MemorySearchNoIndexResults(),
    M0028MemoryEditV2IntentPipeline(),
    M0029PostReviewerLabelStability(),
    M0030StrictTargetAnomalySignals(),
    M0031MemorySearchTwoStageConfigurableLimits(),
    M0032DeleteFileIndexSync(),
    M0033MemorySearchZhTw(),
    M0034MemoryEditOrderingRule(),
    M0035ScopeBoundaryPrompts(),
    M0036MemoryShortTermMove(),
    M0037ContextWindowBoot(),
    M0038SkillsFirstShell(),
    M0039LongTermMemory(),
    M0040PersonaTrigger(),
    M0041MemoryEditOverwrite(),
    M0042VisionAgent(),
    M0043PeopleFolder(),
    M0044PeopleSearchTrigger(),
    M0045MultiIntentPreference(),
    M0046GuiAgents(),
    M0047SessionReorganize(),
    M0048GuiReportProblem(),
    M0049GuiResumeState(),
    M0050BrainScreenshot(),
    M0051GuiObstacleAwareness(),
    M0052GuiUnautomatableEscalation(),
    M0053GuiScrollAwareness(),
    M0054GuiHumanBrowsing(),
    M0055GuiForceToolCall(),
    M0056BrainToolImmediate(),
    M0057GuiRightClickMaximize(),
    M0058GuiScrollKeys(),
    M0059GuiPromptRewrite(),
    M0060BrainPromptV2(),
    M0061GuiTaskGuidance(),
    M0062PeopleProfileSplit(),
    M0063GuiScanLayout(),
    M0064ReadImageBySubagent(),
    M0065GuiScrollDrag(),
    M0066ProgressReviewer(),
    M0067CompletionReviewerPrompts(),
    M0068GuiScrollLayoutPrompts(),
    M0069AgentOsDirAwareness(),
    M0070MemorySyncPrompt(),
    M0071RemoveReviewerShutdown(),
    M0072SenderAwareMessages(),
    M0073ConversationalDefault(),
    M0074GmailAdapter(),
    M0075SendMessage(),
    M0076SendMessageStrict(),
    M0077GuiAppPrompt(),
    M0078SendMessageAttachments(),
    M0079Heartbeat(),
    M0080ThreadRegistry(),
    M0081ThreadPromptRefine(),
    M0082ScheduleFollowup(),
    M0084BootContextSplit(),
    M0085MergeRecentMemory(),
    M0086Bm25MemorySearch(),
    M0087MemoryEditIndexWarnings(),
    M0088MemoryMaintenanceStreamJson(),
    M0089ScreenshotBySubagent(),
    M0090RemoveKernelTimezone(),
    M0091DiscordAdapter(),
    M0092DiscordPromptStyle(),
    M0093DiscordPromptSingleLine(),
    M0094DiscordPromptExamples(),
    M0096BrainPromptEdgeCases(),
    M0097ProviderThinkingMappingFix(),
    M0098BrainRelationshipCorrectness(),
    M0099BrainCooldownNaturalCare(),
    M0100BrainStateSignalFraming(),
    M0101BrainPromptRestructure(),
    M0102BrainPromptIronRulesRefresh(),
    M0103CopilotReasoningVisibility(),
    M0104BrainPromptThinkThenAct(),
    M0105ImpulseSystem(),
    M0106VisionNoHallucination(),
    M0107GuiScrollPosition(),
    M0108GuiTaskBackground(),
    M0109BuiltinSkills(),
    M0110BrainSendMessageSegments(),
    M0111SendMessageSingleBody(),
    M0112SendMessageParallel(),
    M0113DiscordMarkdownPrompt(),
    M0114DiscordBuiltinSkill(),
    M0115DiscordPresentationStrategy(),
    M0116DiscordNaturalLists(),
    M0117DiscordMessageEconomy(),
    M0118SkillPrerequisiteMetadata(),
    M0119DiscordDmSingleLine(),
    M0120ShellNonInteractive(),
    M0121ShellTask(),
    M0122WebSearch(),
    M0123ShellTaskHandoff(),
    M0124DiscordEmojiNewline(),
    M0125BrainPromptFragments(),
    M0126RemoveMemorySearcher(),
    M0127WebFetch(),
    M0128GuiLoadingScrollPrompts(),
    M0129MemoryEditorLongTermRouting(),
    M0130MemoryEditorLongTermStructureGuard(),
    M0131LongTermLists(),
    M0132LongTermCoreValues(),
    M0133AgentTaskNote(),
    M0134DiscordAttachmentContext(),
    M0135SkillCreateGuide(),
    M0136SkillMdFormat(),
    M0137SkillInstallerRepoAtSkill(),
    M0138PersonalSkillsRoot(),
    M0139SkillCheckerAgent(),
    M0140MemoryMaintenanceBuiltin(),
    M0141EndOfTurnTool(),
    M0142WorkerSubagent(),
    M0143BrainWorkerToolDocs(),
    M0144WebFetchPromptDocs(),
    M0145WorkerEnvRules(),
    M0146AppleAppsContext(),
    M0147ICloudSyncPromptFragment(),
    M0148RemoveAppleAppsAutoSync(),
    M0149AppleNotesCache(),
    M0150NotesTemplateMarkdown(),
    M0151NotesTemplateTitleSemantics(),
    M0152NotesTitleBodyRules(),
    M0153SelfImprovementPrompt(),
    M0154MailToolPrompt(),
    M0155StateCommitToolBudget(),
    M0156ScheduleActionBatch(),
    M0157TempMemoryAppendOnly(),
    M0158HeartbeatReliabilityPrompt(),
    M0159RemindersDueTimezoneFix(),
    M0160AxFirstGui(),
    M0161BackupScopeKernelOnly(),
    M0162DiscordInboundStructure(),
    M0163BrainShellDelegation(),
    M0164MemoryMaintenanceWorkerNative(),
    M0165WorkerAsyncDispatch(),
    M0166WorkerMemoryFileAccess(),
    M0167TempMemoryDeltaWrite(),
    M0168RemoveGuiDisplayLimits(),
    M0169MemoryCurationWarnings(),
    M0170MemoryCuration(),
    M0171RemoveMemoryCuratorAgent(),
    M0172CompactorAgent(),
]
