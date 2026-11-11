# Enterprise Text-to-SQL Analytics Agent

Built a local analytics assistant that turns a business question into proposed SQL,
executes it through a restricted SQLite connection, and explains the returned rows.
The model runs through Ollama with Qwen3 4B. It can ask for clarification before
execution, and the database authorizer blocks unapproved reads and writes.

The synthetic sales demo covers a correct joined revenue answer, an ambiguous ranking
question, and a forbidden schema read. Seven actual local acceptance cases passed.
The test suite recorded 182 passing tests and three optional hosted-SDK skips.
Earlier model mistakes are preserved alongside the successful results.

- [Repository](https://github.com/RyanJBush/enterprise-text-to-sql-agent)
- [Demo](media/text2sql-demo.mp4)
- [Verified results](../evidence/results.md)
- [Test output](../evidence/test-output.txt)

Python, SQLite, Ollama, Qwen3, structured response validation, and resource limits.
