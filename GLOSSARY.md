# Study Engine

A personal study planner that adapts learning work to available time without accumulating catch-up debt.

## Language

**Curriculum**:
The approved collection of learning topics and their prerequisite relationships.
_Avoid_: Roadmap (when referring to the approved collection)

**Topic**:
A subject to learn that may require several study blocks.
_Avoid_: Task (when referring to a learning subject)

**Study block**:
A bounded study session devoted to a topic; completing it does not by itself complete the topic.
_Avoid_: Topic (when referring to a single session)

**Topic completion**:
The learner's explicit declaration that a topic is complete, satisfying that topic's prerequisite requirement for dependent topics.
_Avoid_: Block completion (when referring to topic completion)

**Daily queue**:
The ordered study blocks for one local study date, not fixed-time appointments. Unfinished blocks expire with that date without creating catch-up debt.
_Avoid_: Calendar schedule

**Started topic**:
An incomplete topic with positive recorded study time under its own stable identity. A zero-minute session does not make a topic started.

**Study time**:
The actual time the learner spent studying, independent of planned block duration or topic completion.
_Avoid_: Planned duration (when referring to actual time)

**Effort estimate**:
An advisory estimate of the study time a topic may require, not a completion threshold or proof of mastery.
_Avoid_: Required minutes

**Prerequisite**:
A topic that must be explicitly complete before a dependent topic is eligible; every listed prerequisite is required.

**Eligible topic**:
An incomplete topic in the approved curriculum whose prerequisites are all complete. Exceeding its effort estimate does not remove eligibility.

**Backlog**:
Unfinished learning work eligible for consideration in future plans, without an obligation to increase later study time to compensate for missed sessions.
_Avoid_: Schedule debt
