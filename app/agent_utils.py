
from langchain.prompts.chat import (
    ChatPromptTemplate,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate,
)
from langchain.chat_models import ChatOpenAI
from langchain.agents import Tool, AgentOutputParser, AgentExecutor,LLMSingleActionAgent,AgentOutputParser
from langchain.prompts import StringPromptTemplate
from langchain import LLMChain
from typing import List, Optional, Union, Tuple
from langchain.schema import AgentAction, AgentFinish, OutputParserException
import re

import config
from langchain.callbacks.streaming_stdout_final_only import (
    FinalStreamingStdOutCallbackHandler,
)
from data_utils import connect_db, aconnect_db
import text_templates
import chainlit as cl
from bs4 import BeautifulSoup
import requests

class CustomOutputParser(AgentOutputParser):

    def parse(self, llm_output: str) -> Union[AgentAction, AgentFinish]:
        # Check if agent should finish
        if "Final Answer:" in llm_output:
            return AgentFinish(
                # Return values is generally always a dictionary with a single `output` key
                # It is not recommended to try anything else at the moment :)
                return_values={"output": llm_output.split("Final Answer:")[-1].strip()},
                log=llm_output,
            )
        # Parse out the action and action input
        regex = r"Action\s*\d*\s*:(.*?)\nAction\s*\d*\s*Input\s*\d*\s*:[\s]*(.*)"
        match = re.search(regex, llm_output, re.DOTALL)
        if not match:
            raise OutputParserException(f"Could not parse LLM output: `{llm_output}`")
        action = match.group(1).strip()
        action_input = match.group(2)
        # Return the action and action input
        return AgentAction(tool=action, tool_input=action_input.strip(" ").strip('"'), log=llm_output)

# Set up a prompt template
class CustomPromptTemplate(StringPromptTemplate):
    """ Creates a custom prompt template for the agent

    Args:
        StringPromptTemplate (_type_): String template

    Returns:
        StringPromptTemplate: PromptTemplateClass
    """    
    # The template to use
    template: str
    # The list of tools available
    tools: List[Tool]

    def format(self, **kwargs) -> str:
        # Get the intermediate steps (AgentAction, Observation tuples)
        # Format them in a particular way
        intermediate_steps = kwargs.pop("intermediate_steps")
        thoughts = ""
        for action, observation in intermediate_steps:
            thoughts += action.log
            thoughts += f"\nObservation: {observation}\nThought: "
        # Set the agent_scratchpad variable to that value
        kwargs["agent_scratchpad"] = thoughts
        # Create a tools variable from the list of tools provided
        kwargs["tools"] = "\n".join([f"{tool.name}: {tool.description}" for tool in self.tools])
        # Create a list of tool names for the tools provided
        kwargs["tool_names"] = ", ".join([tool.name for tool in self.tools])
        return self.template.format(**kwargs)


def get_chat_template(
    system_prompt = text_templates.OPEN_AI_TEMPLATE,
                      human_prompt = text_templates.HUMAN_TEMPLATE,
                      ) -> ChatPromptTemplate:

    
    system_message_prompt = SystemMessagePromptTemplate.from_template(system_prompt)
    human_message_prompt = HumanMessagePromptTemplate.from_template(human_prompt)


    CHAT_PROMPT = ChatPromptTemplate.from_messages(
        [system_message_prompt, human_message_prompt]
    )
    return CHAT_PROMPT

# Set up the base template

def custom_filter_chain(question:str, 
                        db = connect_db(config.COLLECTION_SUMMARY)
                        ) -> str:
    docs = db.similarity_search(question, k = 1)
    most_likely_doc = docs[0].metadata['source']
    content = docs[0].page_content
    return most_likely_doc , content

async def acustom_filter_chain(question:str, 
                        db = aconnect_db(config.COLLECTION_SUMMARY)
                        ) -> Tuple[str,str]:
    docs = await db.asimilarity_search(question, k = 3)
    most_likely_doc = docs[0].metadata['source']
    content = docs[0].page_content
    return most_likely_doc, content

async def acustom_doc_retrieval(question:str, 
                        db = aconnect_db(config.COLLECTION_SUMMARY),
                        ) -> Tuple[str,str]:
    docs = await db.asimilarity_search(question, k = 1)
    source_docs = [doc.metadata['source'] for doc in docs]
    title = [doc.page_content for doc in docs]
    return source_docs, title

def get_llm(
    model_name:str= config.OPENAI_MODEL,
    **kwargs
    ) -> ChatOpenAI:
    
    llm = ChatOpenAI(temperature=0,
                     model_name=model_name, 
                     max_tokens=4_000,
                     streaming=True, 
                     #callbacks=[FinalStreamingStdOutCallbackHandler()],
                     openai_api_key=config.OPENAI_API_KEY,
                     **kwargs) # 4_000 max tokens in the request, 16k model to avoid context loss
    return llm

def custom_qa(question:str, 
              db = connect_db(config.COLLECTION_CHUNKS),
              llm = get_llm(),
              prompt = get_chat_template()
              ) -> dict[str,str,str]:
    response_dict = {}
    likely_doc , content = custom_filter_chain(question)
    docs = db.similarity_search(question, k = 10, filter={"source":likely_doc})
    # Stuffing the content in a single window
    doc_list = [doc.page_content for doc in docs]
    doc_list.append(content)
    chat_prompt_openai= prompt.format_prompt(question=question, context="\n".join(doc_list)).to_messages()
    response = llm(chat_prompt_openai)
    response_dict['response'] = response
    response_dict['docs'] = docs
    response_dict['chat_prompt_openai'] = chat_prompt_openai
    return response_dict


def get_tools(
    ) -> List[Tool]:
    
    def greeting(query):
        return f"Hola <nombre>, Soy PolicyGuru AI. Puedo responder preguntas sobre las polizas de QuePlan."
    
    def get_news(query):
        headers = {
            "User-Agent":
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.4951.54 Safari/537.36"
        }
        response = requests.get(
            f"https://www.google.com/search?q={query}&gl=cl&tbm=nws&num=20", headers=headers
        )
        soup = BeautifulSoup(response.content, "html.parser")

        news_results = []
        
        for el in soup.select("div.SoaBEf"):
            news_results.append(
                {
                    "title": el.select_one("div.MBeuO").get_text(),
                    "snippet": el.select_one(".GI74Re").get_text(),
                    "link": el.find("a")["href"],
                    "date": el.select_one(".LfVVr").get_text(),
                    "source": el.select_one(".NUnG9d span").get_text()
                }
            )
        news_string = [f"Titulo\n{news['title']}\nTexto\n{news['snippet']}\nlink\n{news['link']}\nfecha\n{news['date']}\nfuente\n{news['source']}" for news in news_results]
        news_text = "\n\n".join(news_string)
        return news_text
    
    # Directly answer the question
    def qa_tool(question:str) -> str:
        response_dict = custom_qa(question)
        response = response_dict['response']
        return response.content

    def qa_tool2(question:str,
                 db = connect_db(config.COLLECTION_CHUNKS)
                 ) -> str:
        likely_doc, content = custom_filter_chain(question)
        docs = db.similarity_search(question, k = 6, filter={"source":likely_doc})
        docs_list = [doc.page_content for doc in docs]
        docs_list.append(content)
        context="\n".join(docs_list)
        return context
    
    async def aqa_tool2(question:str,
                        db = aconnect_db(config.COLLECTION_CHUNKS)
                        ) -> str:
        
        likely_doc, content = await acustom_filter_chain(question)
        docs = await db.asimilarity_search(question, k = 6, filter={"source":likely_doc})
        docs_list = [doc.page_content for doc in docs]
        docs_list.append(content)
        context=  "\n".join(docs_list)
        return context
    
    tools = [
        Tool(
            name = "Web Search",
            func=get_news,
            description="useful for when you need to answer questions about current events, news or the current state of the world. Do not use it when asked about specific policies, in that case, use the policy search tool",
            coroutine=cl.make_async(get_news)
        ),
        Tool(
            name = "Policy Search",
            func=qa_tool2,
            description="useful for when you need to answer questions about an specific policy, in that case, use the policy search tool",
            coroutine=aqa_tool2
        ),
        Tool(
            name = "Greeting",
            func=greeting,
            description="Use this tool only to greet the user and introduce yourself",
            coroutine=cl.make_async(greeting)
        ),
    ]

    return tools

def get_agent_prompt(
    template:str = text_templates.AGENT_TEMPLATE, 
    tools:List[Tool] = get_tools(), 
    input_vars:List[str] = ["input", "intermediate_steps", "history"]
    ) -> CustomPromptTemplate:
    
    prompt = CustomPromptTemplate(
        template=template,
        tools=tools,
        # This omits the `agent_scratchpad`, `tools`, and `tool_names` variables because those are generated dynamically
        # This includes the `intermediate_steps` variable because that is needed
        input_variables=input_vars,
    )
    return prompt

def create_agent(
    prompt:CustomPromptTemplate = get_agent_prompt(),
    output_parser:CustomOutputParser = CustomOutputParser(),
    llm:ChatOpenAI = get_llm(),
    tools:List[Tool] = get_tools(),
    ) -> AgentExecutor:

    llm_chain = LLMChain(llm=llm, prompt=prompt)
    tool_names = [tool.name for tool in tools]
    agent = LLMSingleActionAgent(
        llm_chain=llm_chain,
        output_parser=output_parser,
        stop=["\nObservation:"],
        allowed_tools=tool_names
    )
    agent_executor = AgentExecutor.from_agent_and_tools(agent=agent, 
                                                        tools=tools, 
                                                        verbose=False,
                                                        handle_parsing_errors=True)
    return agent_executor

class ChatBOT():
    chat_history = []
    answer = ""
    db_query  = ""
    db_response = None
    
    def __init__(self):
        self.agent = create_agent()
        
    def get_related_docs(self):
        self.db_response = custom_filter_chain(self.db_query)
        return self.db_response
    
    async def aget_related_docs(self):
        self.db_response = await acustom_doc_retrieval(self.db_query)
        return self.db_response
    
    def clr_history(self):
        self.chat_history = []
    
    def clr_source(self):
        self.db_response = None
        
    def chat(self, query, **kwargs):
        self.db_query = query
        question = {f'history': {'\n'.join(self.chat_history)}, 'input': f"{self.db_query}"}
        self.answer = self.agent.run(question, **kwargs)
        self.chat_history.append(f"Preguntas anteriores: {question['input']}, Respuestas anteriores: {self.answer}")
        return self.answer , self.chat_history, self.get_related_docs()
    
    async def achat(self, query, **kwargs):
        self.db_query = query
        question = {f'history': {'\n'.join(self.chat_history)}, 'input': f"{self.db_query}"}
        response = await self.agent.arun(question, **kwargs)
        # source, content = await self.aget_related_docs()
        # self.answer = response + "\n" + "Source: " + set(source)
        self.answer = response
        self.chat_history.append(f"Preguntas anteriores: {question['input']},\n\n Respuestas anteriores: {self.answer}")
        return self.answer