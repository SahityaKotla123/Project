import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from server import AgentSession


class InsuranceSopTests(unittest.TestCase):
    def test_sample_verifies_and_remembers_claim_hint(self):
        agent = AgentSession()
        reply = agent.receive("I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.")
        self.assertTrue(agent.verified)
        self.assertEqual(agent.phase, "PROCESS_CASE")
        self.assertEqual(agent.selected_claim["case_id"], "CL-2048")
        self.assertIn("verified", reply.lower())

    def test_claim_details_are_blocked_before_verification(self):
        agent = AgentSession()
        reply = agent.receive("Why was my claim denied?")
        self.assertFalse(agent.verified)
        self.assertIsNone(agent.selected_claim)
        self.assertNotIn("pathology", reply.lower())

    def test_three_matching_fields_are_required(self):
        agent = AgentSession()
        agent.receive("My name is Margaret Chen and my policy is POL-9921")
        self.assertFalse(agent.verified)
        agent.receive("My date of birth is 1985-03-15")
        self.assertFalse(agent.verified)
        agent.receive("My SSN last four is 4472")
        self.assertTrue(agent.verified)

    def test_out_of_scope_retries_escalate(self):
        agent = AgentSession()
        for _ in range(3):
            reply = agent.receive("What is RL?")
        self.assertIn("human representative", reply.lower())

    def test_denial_answer_is_grounded_after_verification(self):
        agent = AgentSession()
        agent.receive("Margaret Chen, POL-9921, DOB 1985-03-15, SSN last four 4472")
        reply = agent.receive("Why was the claim denied?")
        self.assertIn("pathology report", reply.lower())
        self.assertIn("CL-2048", reply)


if __name__ == "__main__":
    unittest.main()
