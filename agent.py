import sys
from uuid import uuid4
from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate
import git #gitPython
import os
import time
from tree_sitter import Parser, Node
from tree_sitter_languages import get_language
import detect_language
from dt_diff_lib import extract_data, collect_code_comment_range, tree_sitter_parser_init
import csv

runs = 0
success_runs = 0



def main():

    repo = git.Repo(".")

    #to accommodate test-environment: the test script manages branch name and creation
    branch_id = uuid4()
    branch_name = "Update-docs-" + str(branch_id)
    repo.git.branch(branch_name)
    repo.git.checkout(branch_name)


    # Set the branch name in the GitHub Actions environment
    with open(os.getenv('GITHUB_ENV'), "a") as env_file:
        env_file.write(f"BRANCH_NAME={branch_name}\n")

    # Compare changes and find changed files
    hcommit = repo.head.commit

    diff_files = list(hcommit.diff("HEAD~1"))

    for file in diff_files:
        source_path = str(file.a_path)
        print("Generating comments for diffs in" + source_path)
        file_language = detect_language.detect_language(source_path)
        if not file_language:
                continue
        h1_content = ""
        try:
            h1_commit = repo.commit("HEAD~1")
            h1_blob = h1_commit.tree / source_path
            h1_content = h1_blob.data_stream.read().decode("utf-8")
        except Exception as e:
            print(e)
            print("new file incoming!!")


        with open(source_path, "r") as f:
            source_code = f.read()

        comment_location = run_llm(file_language, h1_content, source_code)

        commented_code = bytearray(source_code.encode("utf-8"))
        for comment, start_byte, end_byte in reversed(comment_location):
            commented_code[start_byte:end_byte] = (comment+"\n").encode()

        # Write changes to docs
        with open(source_path, "w") as f:
            f.write(commented_code.decode("utf-8"))

        # Add changes
        add_files = [source_path]
        repo.index.add(add_files)

    print("{success_runs}/{runs}")

    fail_rate = os.path.join("fail_rate.csv")
    with open(fail_rate, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not os.path.exists(fail_rate):
            writer.writerow(["Successfull runs", "Total runs"])
        writer.writerow([success_runs, runs])

    repo.index.add(fail_rate)

    # Commit changes
    repo.index.commit("Updated inline documentation")

    repo.remotes.origin.push(refspec=f"{branch_name}:{branch_name}",set_upstream=True)

    repo.__del__()
    exit(0)

def validate_response_as_comment(language, response):
    """
    Validates whether a given `response` can be interpreted as a comment in the specified `language`.

    This function parses the response using a Tree-sitter parser and checks:
    - If the parsed tree consists of a single node:
        - It must be a comment (`comment` or `block_comment`), or
        - An expression statement containing a properly enclosed string literal (triple-quoted).
    - If there are multiple nodes:
        - Every node must either be a comment or a block comment to be considered valid.

    Args:
        language: The programming language to be used by the Tree-sitter parser.
        response (str): The response text to validate.

    Returns:
        bool: True if the response is valid as a comment, False otherwise.
    """
    root = tree_sitter_parser_init(language, response.encode("utf-8"))
    children = root.children
    children_len = len(children)
    if children_len == 1:
        child = children[0]    
        return child.type in ["comment", "block_comment"] or (child.type == "expression_statement" and child.children[0].type == "string" and ((child.children[0].text.startswith(b'"""') and child.children[0].text.endswith(b'"""')) or (child.children[0].text.startswith(b"'''") and child.children[0].text.endswith(b"'''"))))
    else: 
        # All children must be comments or block_comments
        return all(child.type in ["comment", "block_comment"] for child in children)


def generate_llm_response(file_language, code, old_comment):
     # Create prompt for LLM
        prompt = ChatPromptTemplate.from_template(
            """
            You are a documentation assistant.

            ## Instructions:
            - Write a function-level documentation for the provided function, following best documentation practice for {program_language}
            Return **only** the comment

            ## Code:
            {code}
            """
        )

        prompt_input = prompt.format(
            code = code,
            program_language = file_language,
        )

        # the LLM does it work
        llm = ChatOllama(model="llama3.2", temperature=0.0)
        llm_response = llm.invoke(prompt_input)
        return llm_response
    
    

def run_llm(file_language, h1_content, source_code):
    global runs, success_runs
    # Extract all function which is in the diff
    code_location = extract_data(True, file_language, h1_content, source_code, collect_code_comment_range)

    comment_location =[]
    for code, old_comment, start_byte, end_byte in code_location:
        runs += 1
        llm_response = generate_llm_response(file_language, code, old_comment)
        if validate_response_as_comment(file_language, llm_response.content):
            success_runs += 1
            comment_location.append(((llm_response.content), start_byte, end_byte))
    
    return comment_location


if __name__ == "__main__":
    main()

