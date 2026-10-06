"""Questions with known answerability per fixture set, shared by tests and calibration."""
SOURDOUGH = "sourdough.txt"
POLICY = "remote_policy.txt"

# (question, file expected to contain the answer)
ANSWERABLE = [
    ("How often should I feed my sourdough starter?", SOURDOUGH),
    ("What does grey liquid on top of the starter mean?", SOURDOUGH),
    ("What temperature should the Dutch oven be for baking the loaf?", SOURDOUGH),
    ("Who is eligible to work remotely?", POLICY),
    ("How much is the monthly internet stipend?", POLICY),
    ("How quickly must a security incident be reported?", POLICY),
]

# Questions answerable from neither document
UNANSWERABLE = [
    "Who won the football world cup in 2010?",
    "What is the capital of Australia?",
    "Explain how photosynthesis works in plants.",
    "How do I configure a PostgreSQL replication slot?",
]
