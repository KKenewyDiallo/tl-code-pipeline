"""Génération du rapport QA pour les commentaires Jira.

Ce module fournit une fonction pour formater un rapport structuré
pour l'équipe QA, incluant : résumé, fichiers modifiés, critères d'acceptation,
résultats des tests et instructions de test manuel.
"""

import re
from typing import Any


def _extract_summary(spec_content: str) -> str:
    """Extrait un résumé de 1-2 phrases depuis SPEC.md.
    
    Cherche une section 'Résumé', 'Summary', ou utilise les premières lignes.
    """
    if not spec_content:
        return "Aucun résumé disponible"
    
    # Chercher une section Résumé/Summary
    patterns = [
        r"##\s*(?:Résumé|Summary|Contexte|Context)\s*\n+(.+?)(?=\n\n##|\n#|\Z)",
        r"(?:Résumé|Summary|Contexte|Context):\s*(.+?)(?=\n\n|\n#|\n##|\Z)",
        r"^#.*?\n\n(.+?)(?=\n\n|\n#|\n##|\Z)",
    ]
    
    for pattern in patterns:
        match = re.search(pattern, spec_content, re.DOTALL | re.IGNORECASE)
        if match:
            text = match.group(1).strip()
            # Nettoyer le texte : enlever les marqueurs markdown
            text = re.sub(r"^#+\s*", "", text)
            text = re.sub(r"^[-*•]\s*", "", text)
            # Garder seulement 1-2 phrases
            sentences = re.split(r"[.!?]\s+", text)
            result = ". ".join(sentences[:2]).strip()
            if len(sentences) > 0 and not text.endswith((".", "!", "?")):
                result += "."
            return result
    
    # Sinon, prendre les premières lignes
    lines = spec_content.split("\n")
    non_empty_lines = [l.strip() for l in lines if l.strip()]
    if non_empty_lines:
        # Prendre les 2 premières lignes non vides
        summary = " ".join(non_empty_lines[:2])
        # Tronquer à 200 caractères max
        return summary[:200].strip() + "..." if len(summary) > 200 else summary.strip()
    
    return "Aucun résumé disponible"


def _extract_acceptance_criteria(spec_content: str) -> list[dict]:
    """Extrait les critères d'acceptation sous forme de scénarios QUAND/ALORS ou WHEN/THEN.
    
    Retourne une liste de dicts avec 'id', 'scenario', 'status' ('non testé').
    """
    criteria = []
    if not spec_content:
        return criteria
    
    # Chercher une section dédiée (## ou ###)
    section_pattern = r"(?:#{2,3}\s*(?:Critères? d'(?:acceptation|accéptation)|Acceptance Criteria?|Scénarios?)\s*\n\n)(.+?)(?=\n\n#|\Z)"
    section_match = re.search(section_pattern, spec_content, re.DOTALL | re.IGNORECASE)
    
    if section_match:
        content = section_match.group(1)
    else:
        content = spec_content
    
    # Extraire les scénarios QUAND...ALORS ou WHEN...THEN
    # Pattern pour QUAND/ALORS sur des lignes consécutives
    scenario_pattern = r"(?:^|\n)\s*(?:QUAND|WHEN)\s+(.+?)\s*\n\s*(?:ALORS|THEN)\s+(.+?)\s*(?=\n|$)"
    scenarios = re.findall(scenario_pattern, content, re.DOTALL | re.IGNORECASE | re.MULTILINE)
    
    for i, (when, then) in enumerate(scenarios, 1):
        scenario_text = f"QUAND {when.strip()} ALORS {then.strip()}"
        criteria.append({
            "id": i,
            "scenario": scenario_text,
            "status": "non testé"
        })
    
    # Si pas de scénarios trouvés, essayer de trouver des lignes avec QUAND/ALORS/WHEN/THEN
    if not criteria:
        lines = content.split("\n")
        current_when = None
        for line in lines:
            line = line.strip()
            when_match = re.match(r"(?:QUAND|WHEN)\s+(.+)", line, re.IGNORECASE)
            if when_match:
                current_when = when_match.group(1).strip()
                continue
            then_match = re.match(r"(?:ALORS|THEN)\s+(.+)", line, re.IGNORECASE)
            if then_match and current_when:
                scenario_text = f"QUAND {current_when} ALORS {then_match.group(1).strip()}"
                criteria.append({
                    "id": len(criteria) + 1,
                    "scenario": scenario_text,
                    "status": "non testé"
                })
                current_when = None
    
    return criteria


def _extract_manual_test_steps(spec_content: str) -> list[str]:
    """Extrait les étapes de test manuel depuis SPEC.md.
    
    Cherche une section 'Test manuel', 'Manual testing', etc.
    """
    steps = []
    if not spec_content:
        return ["Vérifier le fonctionnement général de la fonctionnalité modifiée"]
    
    patterns = [
        r"(?:Test manuel|Manual testing|Comment tester|How to test):\s*\n+(.+?)(?=\n\n#|\Z)",
        r"#{2,3}\s*(?:Test manuel|Manual testing|Comment tester|How to test)\s*\n+(.+?)(?=\n\n#|\Z)",
    ]
    
    for pattern in patterns:
        match = re.search(pattern, spec_content, re.DOTALL | re.IGNORECASE)
        if match:
            content = match.group(1).strip()
            # Extraire les étapes (lignes commençant par - , *, •, ou numéro)
            step_lines = re.findall(r"^\s*[-*•]\s+(.+?)$|^\s*[0-9]+[.)]\s+(.+?)$", content, re.MULTILINE)
            for step_tuple in step_lines:
                step = step_tuple[0] or step_tuple[1]
                if step.strip():
                    steps.append(step.strip())
            if steps:
                return steps
    
    # Si pas trouvé, retourner une instruction générique
    return ["Vérifier le fonctionnement général de la fonctionnalité modifiée"]


def _extract_modified_files(diff_output: str) -> list[str]:
    """Extrait la liste des fichiers modifiés depuis un diff git."""
    if not diff_output:
        return []
    
    files = set()
    # Format diff: diff --git a/file1 b/file2
    for line in diff_output.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 4:
                # a/file et b/file
                old_file = parts[2][2:]  # enlève 'a/'
                new_file = parts[3][2:]  # enlève 'b/'
                # Prendre le nouveau nom (pour les renommages)
                if old_file != new_file:
                    files.add(new_file)
                else:
                    files.add(old_file)
    
    return sorted(list(files))


def _format_test_results(test_results: dict) -> list[dict]:
    """Formate les résultats des tests pour le rapport.
    
    test_results est le dict retourné par test_runner.run_tests()
    """
    results = []
    
    if not test_results:
        return [{"name": "Aucun test", "status": "⚠️ Aucun test automatique exécuté — test manuel obligatoire"}]
    
    if test_results.get("unrunnable"):
        return [{"name": "Tests", "status": "⚠️ Aucun test automatique exécuté — test manuel obligatoire"}]
    
    if "log" in test_results:
        log = test_results["log"]
        # Parser les résultats de pytest
        # Format: test_file::test_name PASSED/FAILED
        passed_tests = []
        failed_tests = []
        
        for line in log.split("\n"):
            if " PASSED" in line and "test_" in line:
                test_name = line.split(" PASSED")[0].strip()
                if test_name and not test_name.startswith("==="):
                    passed_tests.append(test_name)
            elif " FAILED" in line and "test_" in line:
                test_name = line.split(" FAILED")[0].strip()
                if test_name and not test_name.startswith("==="):
                    failed_tests.append(test_name)
        
        for test in passed_tests:
            results.append({"name": test, "status": "✅ PASSED"})
        for test in failed_tests:
            results.append({"name": test, "status": "❌ FAILED"})
    
    if not results:
        results.append({"name": "Tests", "status": "⚠️ Aucun test automatique exécuté — test manuel obligatoire"})
    
    return results


def format_qa_report(
    spec_content: str,
    review_link: str,
    diff_output: str,
    test_results: dict,
) -> str:
    """Génère un rapport QA structuré pour Jira.
    
    Args:
        spec_content: Contenu de SPEC.md
        review_link: URL de la MR
        diff_output: Output de git diff
        test_results: Dict des résultats des tests (de test_runner.run_tests)
    
    Returns:
        str: Commentaire Jira formaté en markdown
    """
    # Extraire les informations
    summary = _extract_summary(spec_content)
    criteria = _extract_acceptance_criteria(spec_content)
    modified_files = _extract_modified_files(diff_output)
    test_results_formatted = _format_test_results(test_results)
    manual_steps = _extract_manual_test_steps(spec_content)
    
    # Construire le rapport
    lines = []
    lines.append("## 📋 Rapport pour la QA")
    lines.append("")
    
    # 1. Résumé
    lines.append("**Résumé** : " + summary)
    lines.append("")
    
    # 2. Lien MR
    lines.append(f"**MR** : {review_link}")
    lines.append("")
    
    # 3. Fichiers modifiés
    lines.append("**Fichiers modifiés** :")
    if modified_files:
        for f in modified_files:
            lines.append(f"- `{f}`")
    else:
        lines.append("- Aucun fichier modifié détecté")
    lines.append("")
    
    # 4. Critères d'acceptation
    lines.append("**Critères d'acceptation** :")
    if criteria:
        for crit in criteria:
            lines.append(f"- [{crit['status']}] {crit['scenario']}")
    else:
        lines.append("- Aucun critère d'acceptation trouvé dans SPEC.md")
    lines.append("")
    
    # 5. Tests automatiques
    lines.append("**Tests automatiques** :")
    if test_results_formatted:
        for test in test_results_formatted:
            lines.append(f"- {test['name']} : {test['status']}")
    else:
        lines.append("- ⚠️ Aucun test automatique exécuté — test manuel obligatoire")
    lines.append("")
    
    # 6. Test manuel
    lines.append("**Comment tester à la main** :")
    if manual_steps:
        for i, step in enumerate(manual_steps, 1):
            lines.append(f"{i}. {step}")
    else:
        lines.append("1. Vérifier le fonctionnement général de la fonctionnalité modifiée")
    
    return "\n".join(lines)
