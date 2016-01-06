App Engine application for the Udacity training course.

## Products
- [App Engine][1]

## Language
- [Python][2]

## APIs
- [Google Cloud Endpoints][3]

## Setup Instructions
1. Update the value of `application` in `app.yaml` to the app ID you
   have registered in the App Engine admin console and would like to use to host
   your instance of this sample.
1. Update the values at the top of `settings.py` to
   reflect the respective client IDs you have registered in the
   [Developer Console][4].
1. Update the value of CLIENT_ID in `static/js/app.js` to the Web client ID
1. (Optional) Mark the configuration files as unchanged as follows:
   `$ git update-index --assume-unchanged app.yaml settings.py static/js/app.js`
1. Run the app with the devserver using `dev_appserver.py DIR`, and ensure it's running by visiting
   your local server's address (by default [localhost:8080][5].)
1. Generate your client library(ies) with [the endpoints tool][6].
1. Deploy your application.

## Sessions
	Sessions can be created for a given conference as a child of the conference. The Session class can track the name, session highlights, the speaker, the type of session, the duration of the session, the date of the session, and the session's start time.

	Sessions can only be created by the creator of the conference. The name of the session is required to create the session. 

	If the speaker is provided, the speaker must be present in the Speaker class. Only one speaker is currently permitted. Future improvements might allow for multiple registered speakers for a given session.

	startTime is in 24 hour time to allow for sorting of times.

	Users may add or remove a session from the user's wishlist. The user need not be registered for the conference to add the session to the wishlist.

## Speakers
	Speakers can be created by users independent of conferences or sessions, but the speaker is connected to the profile of the user that created the speaker. The speaker class tracks the name, bio and email of the speaker.

	Speakers must be added to the app before they can be added to a session.

## Two Additional Queries

	Get Session by Date
	Using the websafe conference key and a date in the format mmddyyyy, users can query existing sessions for that conference on that date. The date must be in the proper format, be a valid date, and be during the conference (if conference start and end dates exist).

	Get Speaker by City
	If the user provides the name of the city, the app will return the speakers appearing in conferences occuring in that city. The city must be found in conferences in the app.

Problem Query
	"Let’s say that you don't like workshops and you don't like sessions after 7 pm. How would you handle a query for all non-workshop sessions before 7 pm? What is the problem for implementing this query? What ways to solve it did you think of?"

	This query is implemented as getSessionsNotTypeBeforeHour at path:

	conference/{websafeConferenceKey}/session/nottype/{nottype}/before/{hour}

	where nottype is a valid typeOfSession and hour is a number between 1 and 23 representing the hour the user wants to find sessions occuring before.

	The logic of the query is that the function:
		1. creates a list  of typeOfSessions that excludes the given type
		2. finds the sessions that match this exclusive list
		3. limits these sessions to ones that are in the given conference
		4. finally, limits the sessions to those that occur before the given hour
	Steps 1-2, finding the sessions that do not equal the given session type, avoid using an inequity to do so. Instead, we can use the IN method to accomplish this exclusion. That allows step 4, finding sessions that occur before the given hour, to be the only in equity in this query.

## Paths, Methods, and Functions

The apps paths, methods and functions are summarized below:

PATH        							HTTP    Function
conference 								POST	createConference
conference/{websafeConferenceKey}		PUT    	updateConference
conference/{websafeConferenceKey}		GET    	getConference
getConferencesCreated					POST	getConferencesCreated
queryConferences						POST	queryConferences
conference/{websafeConferenceKey}/session	
										GET 	getConferenceSessions
conference/{websafeConferenceKey}/session	
										POST 	createSession
conference/{websafeConferenceKey}/session/type/{typeOfSession}	
										GET 	getConferenceSessionsByType
conference/session/{websafeSessionKey}	POST 	addSessionToWishlist
conference/session/{websafeSessionKey}	DELETE 	deleteSessionInWishlist
session 								GET 	getSessionsInWishlist
speaker/{websafeSpeakerKey}/session 	GET 	getSessionsBySpeaker
conference/{websafeConferenceKey}/session/nottype/{nottype}/before/{hour}
										GET		getSessionsNotTypeBeforeHour
conference/{websafeConferenceKey}/session/date/{sessdate}
										GET 	getSessionsByDate
profile 								GET 	getProfile
profile									POST	saveProfile
speaker									POST	createSpeaker
speaker/city/{city}/get 				GET 	getSpeakerByCity
conference/announcement/get				GET 	getAnnouncement
conference/featuredspeaker/get			GET 	getFeaturedSpeaker
conferences/attending					GET 	getConferencesToAttend
conference/{websafeConferenceKey}		POST	registerForConference
conference/{websafeConferenceKey}		DELETE 	unregisterFromConference
filterPlayground						GET 	filterPlayground

Design Notes

conference/{websafeConferenceKey}		DELETE 	unregisterFromConference
	This is a unexpected design choice, as this path and method would be typically expected to delete the conference, not register it. As it was the original design, I left it untouched.

getConferencesCreated					POST	getConferencesCreated
	This is also an unexpected choice by the original author. It is unclear what is being posted here. I've left the original program untouched.

## Thanks

Thanks to the original author and the Udacity team for the framework, instruction and support to make this amended app possible.

[1]: https://developers.google.com/appengine
[2]: http://python.org
[3]: https://developers.google.com/appengine/docs/python/endpoints/
[4]: https://console.developers.google.com/
[5]: https://localhost:8080/
[6]: https://developers.google.com/appengine/docs/python/endpoints/endpoints_tool
